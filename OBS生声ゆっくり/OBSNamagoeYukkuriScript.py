import numpy as np
import pyaudio
import customtkinter as ctk
import tkinter.messagebox as messagebox
from obswebsocket import obsws, requests
import threading
import string
import re
import json
import os
import queue
import time
import sys

# ====== 設定ファイルとフォルダ ======
PRESET_FOLDER = "presets"
OBS_PRESET_FOLDER = "obs_presets"
THEME_SETTINGS_FILE = "theme_settings.json"
AUTO_LOAD_SETTINGS_FILE = "auto_load_settings.json"

# ====== 定数定義 ======
MAX_RMS_VALUE = 2000
COOLING_TIME = 0.05 # 安定化期間（秒）
MAX_IMAGE_COUNT = 1000 # 検索する画像の最大数

# PyAudio設定
CHUNK = 1024
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 44500

# グローバル変数
run_audio_thread = False
audio_thread = None
obs_client = None
current_scene_name = ""
current_group_name = "" 
current_image_ids = {} # 画像名とIDを格納する辞書
current_threshold_min = 0
current_threshold_max = 0
audio_data_queue = queue.Queue() # 音量データ伝達用のキュー
selected_mic_index = None

# OBS 非同期接続用ラッパー
class AsyncOBS:
    def __init__(self, host, port, password):
        self.ws = obsws(host, port, password)

    def connect(self):
        try:
            self.ws.connect()
            print("✅ OBSに接続成功")
            return True
        except Exception as e:
            print(f"❌ OBSへの接続に失敗しました: {e}")
            return False
            
    def get_scene_list(self):
        try:
            response = self.ws.call(requests.GetSceneList())
            if response.status:
                scene_names = [scene['sceneName'] for scene in response.datain['scenes']]
                return scene_names
            else:
                return []
        except Exception:
            return []
            
    def get_group_list_in_scene(self, scene_name):
        try:
            response = self.ws.call(requests.GetSceneItemList(sceneName=scene_name))
            if response.status:
                # 修正部分: sourceKindとisGroupの両方を確認する
                group_names = [item['sourceName'] for item in response.datain['sceneItems'] if item.get('sourceKind') == 'group' or item.get('isGroup') == True]
                return group_names
            else:
                return []
        except Exception:
            return []

    def get_scene_item_id(self, scene_name, source_name):
        try:
            response = self.ws.call(
                requests.GetSceneItemId(sceneName=scene_name, sourceName=source_name)
            )
            if response.status and 'sceneItemId' in response.datain:
                return response.datain['sceneItemId']
            return None
        except Exception:
            return None

    def set_visible(self, scene_name, item_id, visible):
        try:
            self.ws.call(
                requests.SetSceneItemEnabled(sceneName=scene_name, sceneItemId=item_id, sceneItemEnabled=visible)
            )
        except Exception:
            pass

    def disconnect(self):
        self.ws.disconnect()

# PyAudioデバイス取得関数
def get_mic_devices():
    p = pyaudio.PyAudio()
    info = p.get_host_api_info_by_index(0)
    num_devices = info.get('deviceCount')
    
    devices = []
    for i in range(0, num_devices):
        device_info = p.get_device_info_by_host_api_device_index(0, i)
        if device_info.get('maxInputChannels') > 0:
            devices.append({
                "name": device_info.get('name'),
                "index": i
            })
    p.terminate()
    return devices

# 音量から画像インデックス計算
def get_image_index(volume, n_images):
    index = int(volume * n_images)
    return min(index, n_images - 1)

# オーディオとOBSを操作する関数（別スレッドで実行）
def audio_loop(app_instance):
    global obs_client, run_audio_thread, current_scene_name, current_group_name, current_image_ids, current_threshold_min, current_threshold_max, audio_data_queue, selected_mic_index

    print("🎧 オーディオスレッド開始")

    try:
        obs_client = AsyncOBS(app_instance.obs_host_entry.get(), int(app_instance.obs_port_entry.get()), app_instance.obs_password_entry.get())
        if not obs_client.connect():
            app_instance.after(0, app_instance.on_stop)
            app_instance.after(0, lambda: app_instance.status_label.configure(text="OBS接続エラー", text_color="red"))
            return

        if not current_image_ids:
            print("❌ 画像ソースのIDが取得できていません。")
            obs_client.disconnect()
            app_instance.after(0, app_instance.on_stop)
            app_instance.after(0, lambda: app_instance.status_label.configure(text="画像ソースIDエラー", text_color="red"))
            return
            
        start_index = int(app_instance.image_range_start_optionmenu.get())
        end_index = int(app_instance.image_range_end_optionmenu.get())
        
        # 選択された範囲の画像のみを抽出
        image_names = sorted(current_image_ids.keys(), key=lambda x: int(re.sub(r'[^0-9]', '', x)))
        selected_image_names = [name for name in image_names if start_index <= int(re.sub(r'[^0-9]', '', name)) <= end_index]

        if not selected_image_names:
            print("❌ 選択された範囲に画像ソースが見つかりませんでした。")
            obs_client.disconnect()
            app_instance.after(0, app_instance.on_stop)
            app_instance.after(0, lambda: app_instance.status_label.configure(text="選択範囲に画像なし", text_color="red"))
            return
            
        prev_index = -1
        last_change_time = 0

        # すべての画像を非表示にする
        for image_name in selected_image_names:
            item_id = current_image_ids.get(image_name)
            if item_id is not None:
                obs_client.set_visible(current_group_name, item_id, False)

        p = pyaudio.PyAudio()
        try:
            stream = p.open(format=FORMAT,
                            channels=CHANNELS,
                            rate=RATE,
                            input_device_index=selected_mic_index,
                            input=True,
                            frames_per_buffer=CHUNK)
        except Exception as e:
            print(f"❌ PyAudioデバイスのオープンに失敗しました: {e}")
            obs_client.disconnect()
            app_instance.after(0, app_instance.on_stop)
            app_instance.after(0, lambda: app_instance.status_label.configure(text="マイクエラー", text_color="red"))
            return

        print("🎤 マイク音量取得中…")
        
        while run_audio_thread:
            data = np.frombuffer(stream.read(CHUNK, exception_on_overflow=False), dtype=np.int16)
            rms = np.sqrt(np.mean(np.square(data, dtype=np.float64))) if data.size > 0 else 0.0
            
            audio_data_queue.put(rms)

            if rms < current_threshold_min:
                # 音量閾値以下の場合、一番低い番号の画像を表示する
                index = 0
            else:
                volume = (rms - current_threshold_min) / (current_threshold_max - current_threshold_min)
                index = get_image_index(volume, len(selected_image_names))
            
            current_time = time.time()

            if index != prev_index and (current_time - last_change_time) >= COOLING_TIME:
                if prev_index != -1 and prev_index is not None:
                    item_id_to_hide = current_image_ids.get(selected_image_names[prev_index])
                    if item_id_to_hide is not None:
                        obs_client.set_visible(current_group_name, item_id_to_hide, False)
                
                item_id_to_show = current_image_ids.get(selected_image_names[index])
                if item_id_to_show is not None:
                    obs_client.set_visible(current_group_name, item_id_to_show, True)
                
                prev_index = index
                last_change_time = current_time
            
    except Exception as e:
        print(f"❌ オーディオスレッドで予期せぬエラーが発生しました: {e}")
        app_instance.after(0, app_instance.on_stop)
        app_instance.after(0, lambda: app_instance.show_error(f"オーディオ処理中にエラーが発生しました: {e}"))
        
    finally:
        if 'stream' in locals() and stream.is_active():
            stream.stop_stream()
            stream.close()
        if 'p' in locals():
            p.terminate()
        if obs_client:
            obs_client.disconnect()
        print("✅ オーディオループ終了")

def start_audio_thread(app_instance):
    global run_audio_thread, audio_thread
    if audio_thread is not None and audio_thread.is_alive():
        stop_audio_thread()
    
    run_audio_thread = True
    audio_thread = threading.Thread(target=audio_loop, args=(app_instance,))
    audio_thread.start()
    print("✅ 新しいオーディオスレッドを開始しました。")

def stop_audio_thread():
    global run_audio_thread, audio_thread
    if audio_thread and audio_thread.is_alive():
        run_audio_thread = False
        audio_thread.join()
        print("✅ オーディオスレッドを停止しました。")

# GUIクラス
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("OBS生声ゆっくり")
        self.geometry("500x780")
        self.grid_columnconfigure(0, weight=1)
        
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        if not os.path.exists(PRESET_FOLDER):
            os.makedirs(PRESET_FOLDER)
        if not os.path.exists(OBS_PRESET_FOLDER):
            os.makedirs(OBS_PRESET_FOLDER)
        
        self.current_theme_name = self.load_theme_settings()
        ctk.set_appearance_mode(self.current_theme_name)
        
        self.obs_preset_var = ctk.StringVar(value="OBS接続: なし")
        self.app_preset_var = ctk.StringVar(value="アプリ設定: なし")
        
        self.mic_devices = get_mic_devices()
        self.mic_device_names = [dev["name"] for dev in self.mic_devices]
        
        self.obs_client = None
        self.is_obs_preset_valid = False
        self.is_app_preset_valid = False
        self.is_searching = False # 検索中フラグを追加
        
        # 修正部分: 検索結果をキャッシュする辞書を追加
        self.cache_image_ids = {}

        self.create_widgets()
        
        self.update_preset_list()
        self.update_obs_preset_list()
        self.update_volume_monitor()
        
        self.auto_load_settings = self.load_auto_load_settings()
        # auto_load_checkboxをauto_search_checkboxに名称変更
        self.auto_search_checkbox.select() if self.auto_load_settings.get("auto_load", False) else self.auto_search_checkbox.deselect()

    def create_widgets(self):
        # 既存のテーマ設定フレーム
        appearance_control_frame = ctk.CTkFrame(self, fg_color="transparent")
        appearance_control_frame.pack(fill="x", padx=10, pady=(10, 5))
        self.appearance_label = ctk.CTkLabel(appearance_control_frame, text="テーマ:")
        self.appearance_label.pack(side="left", padx=(0, 5))
        self.appearance_mode_switch = ctk.CTkSwitch(
            appearance_control_frame, text="", command=self.change_appearance_mode
        )
        self.appearance_mode_switch.pack(side="left", padx=(5, 0))
        self.current_theme_label = ctk.CTkLabel(appearance_control_frame, text=f"（{self.current_theme_name}）")
        self.current_theme_label.pack(side="left", padx=(5, 0))
        if self.current_theme_name == "Light":
            self.appearance_mode_switch.select()

        # 追加する「取扱説明書」ボタン
        help_button = ctk.CTkButton(
            appearance_control_frame,
            text="取扱説明書",
            command=self.show_manual,
            width=120  # 幅を調整して他のウィジェットと並べやすくする
        )
        help_button.pack(side="right", padx=(10, 0))

        self.scrollable_frame = ctk.CTkScrollableFrame(self)
        self.scrollable_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        obs_group_frame = ctk.CTkFrame(self.scrollable_frame, corner_radius=10)
        obs_group_frame.pack(padx=10, pady=(0, 5), fill="x")
        ctk.CTkLabel(obs_group_frame, text="OBS接続設定", font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(5, 5))
        
        self.obs_host_entry = self.add_entry_with_label(obs_group_frame, "ホスト:", "localhost", self.clear_obs_preset_name)
        self.obs_port_entry = self.add_entry_with_label(obs_group_frame, "ポート:", "4455", self.clear_obs_preset_name)
        self.obs_password_entry = self.add_entry_with_label(obs_group_frame, "パスワード:", "", self.clear_obs_preset_name)

        # 接続テストボタンの名称変更と自動画像検索チェックボックスの移動
        connection_button_frame = ctk.CTkFrame(obs_group_frame, fg_color="transparent")
        connection_button_frame.pack(fill="x", pady=5)
        ctk.CTkButton(connection_button_frame, text="OBS接続", command=self.test_obs_connection).pack(side="left", padx=(10, 5), expand=True, fill="x")
        # auto_load_checkboxをauto_search_checkboxに名称変更
        self.auto_search_checkbox = ctk.CTkCheckBox(connection_button_frame, text="接続時に自動画像検索", command=self.save_auto_load_settings)
        self.auto_search_checkbox.pack(side="left", padx=(5, 10))

        obs_preset_frame = ctk.CTkFrame(obs_group_frame, fg_color="transparent")
        obs_preset_frame.pack(fill="x", padx=10, pady=5)
        
        self.obs_current_preset_label_container = ctk.CTkFrame(obs_preset_frame, corner_radius=10)
        self.obs_current_preset_label_container.pack(fill="x", padx=10, pady=(0, 5))
        self.obs_current_preset_label = ctk.CTkLabel(self.obs_current_preset_label_container, text="適用中: なし")
        self.obs_current_preset_label.pack(fill="x", padx=10, pady=5)
        
        obs_load_frame = ctk.CTkFrame(obs_preset_frame, fg_color="transparent")
        obs_load_frame.pack(fill="x", padx=10, pady=5)
        ctk.CTkLabel(obs_load_frame, text="プリセット選択:", width=100).pack(side="left", padx=(0, 5))
        self.obs_preset_optionmenu = ctk.CTkOptionMenu(obs_load_frame, values=["-"])
        self.obs_preset_optionmenu.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(obs_load_frame, text="適用", width=60, command=self.load_obs_preset).pack(side="left", padx=(5, 0))
        ctk.CTkButton(obs_load_frame, text="削除", width=60, command=self.delete_obs_preset).pack(side="left", padx=(5, 0))

        obs_save_frame = ctk.CTkFrame(obs_preset_frame, fg_color="transparent")
        obs_save_frame.pack(fill="x", padx=10, pady=5)
        ctk.CTkLabel(obs_save_frame, text="名前を付けて保存:", width=100).pack(side="left", padx=(0, 5))
        self.obs_preset_name_entry = ctk.CTkEntry(obs_save_frame, placeholder_text="新しいプリセット名")
        self.obs_preset_name_entry.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(obs_save_frame, text="保存", width=60, command=self.save_obs_preset).pack(side="left", padx=(5, 0))

        app_group_frame = ctk.CTkFrame(self.scrollable_frame, corner_radius=10)
        app_group_frame.pack(padx=10, pady=(5, 5), fill="x")
        
        preset_frame = ctk.CTkFrame(app_group_frame, fg_color="transparent")
        preset_frame.pack(padx=10, pady=5, fill="x")
        ctk.CTkLabel(preset_frame, text="アプリ設定プリセット", font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(10, 5))
        
        self.app_current_preset_label_container = ctk.CTkFrame(preset_frame, corner_radius=10)
        self.app_current_preset_label_container.pack(fill="x", padx=10, pady=(0, 5))
        self.app_current_preset_label = ctk.CTkLabel(self.app_current_preset_label_container, text="適用中: なし")
        self.app_current_preset_label.pack(fill="x", padx=10, pady=5)
        
        load_frame = ctk.CTkFrame(preset_frame, fg_color="transparent")
        load_frame.pack(fill="x", padx=10, pady=5)
        ctk.CTkLabel(load_frame, text="プリセット選択:", width=100).pack(side="left", padx=(0, 5))
        self.preset_optionmenu = ctk.CTkOptionMenu(load_frame, values=["-"])
        self.preset_optionmenu.pack(side="left", fill="x", expand=True)
        self.load_preset_button = ctk.CTkButton(load_frame, text="適用", width=60, command=self.load_preset)
        self.load_preset_button.pack(side="left", padx=(5, 0))
        self.delete_preset_button = ctk.CTkButton(load_frame, text="削除", width=60, command=self.delete_preset)
        self.delete_preset_button.pack(side="left", padx=(5, 0))
        
        save_frame = ctk.CTkFrame(preset_frame, fg_color="transparent")
        save_frame.pack(fill="x", padx=10, pady=5)
        ctk.CTkLabel(save_frame, text="名前を付けて保存:", width=100).pack(side="left", padx=(0, 5))
        self.preset_name_entry = ctk.CTkEntry(save_frame, placeholder_text="新しいプリセット名")
        self.preset_name_entry.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(save_frame, text="保存", width=60, command=self.save_preset).pack(side="left", padx=(5, 0))
        
        setting_frame = ctk.CTkFrame(app_group_frame, fg_color="transparent")
        setting_frame.pack(padx=10, pady=5, fill="x")
        ctk.CTkLabel(setting_frame, text="シーン・画像・マイク設定", font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(5, 5))

        # auto_load_frameとauto_load_checkboxを削除
        # auto_load_frame = ctk.CTkFrame(setting_frame, fg_color="transparent")
        # auto_load_frame.pack(fill="x", pady=5)
        # self.auto_load_checkbox = ctk.CTkCheckBox(auto_load_frame, text="起動時に自動読み込み", command=self.save_auto_load_settings)
        # self.auto_load_checkbox.pack(side="left")

        scene_frame = ctk.CTkFrame(setting_frame, fg_color="transparent")
        scene_frame.pack(fill="x", pady=5)
        ctk.CTkLabel(scene_frame, text="シーン名:", width=100).pack(side="left", padx=(0, 5))
        # 修正: シーン変更時に画像情報をリセットするメソッドを呼び出す
        self.scene_name_optionmenu = ctk.CTkOptionMenu(scene_frame, values=["-"], command=lambda value: (self.clear_group_and_image_info(value), self.clear_app_preset_status()))
        self.scene_name_optionmenu.bind("<Configure>", lambda event: self.clear_app_preset_status()) # 変更
        self.scene_name_optionmenu.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(scene_frame, text="シーン内画像検索", width=120, command=self.start_find_sources_in_scene_thread).pack(side="left", padx=(5, 0))

        group_frame = ctk.CTkFrame(setting_frame, fg_color="transparent")
        group_frame.pack(fill="x", pady=5)
        ctk.CTkLabel(group_frame, text="グループ名:", width=100).pack(side="left", padx=(0, 5))
        # 修正部分: グループ名変更時に_update_image_range_on_group_changeを呼び出す
        self.group_name_optionmenu = ctk.CTkOptionMenu(group_frame, values=["-"], command=lambda value: (self._update_image_range_on_group_change(value), self.clear_app_preset_status())) 
        self.group_name_optionmenu.bind("<Configure>", lambda event: self.clear_app_preset_status()) # 変更
        self.group_name_optionmenu.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(group_frame, text="グループ内画像検索", width=120, command=self.start_find_sources_in_group_thread).pack(side="left", padx=(5, 0))

        # OBS内画像ID網羅検索ボタンと再起動ボタンを横並びにするためのフレームを追加
        search_and_restart_frame = ctk.CTkFrame(setting_frame, fg_color="transparent")
        search_and_restart_frame.pack(fill="x", pady=5, padx=10)
        
        # 新しい再起動ボタンを追加
        ctk.CTkButton(search_and_restart_frame, text="↻ 再起動", command=self.on_restart).pack(side="left", padx=(0, 5), fill="x", expand=True)
        # 既存のボタンを新しいフレームに移動
        ctk.CTkButton(search_and_restart_frame, text="OBS内画像ID網羅検索", command=self.start_find_all_sources_thread).pack(side="left", padx=(5, 0), fill="x", expand=True)
        
        self.found_images_label = ctk.CTkLabel(setting_frame, text="見つかった画像: 0個")
        self.found_images_label.pack(fill="x", pady=5)
        
        # 画像範囲選択プルダウン
        image_range_frame = ctk.CTkFrame(setting_frame, fg_color="transparent")
        image_range_frame.pack(fill="x", pady=5)
        ctk.CTkLabel(image_range_frame, text="使用画像範囲:", width=100).pack(side="left", padx=(0, 5))
        self.image_range_start_optionmenu = ctk.CTkOptionMenu(image_range_frame, values=["-"], width=80)
        self.image_range_start_optionmenu.bind("<Configure>", lambda event: self.clear_app_preset_status()) # 変更
        self.image_range_start_optionmenu.pack(side="left", padx=(0, 5))
        ctk.CTkLabel(image_range_frame, text="〜", width=20).pack(side="left", padx=0)
        self.image_range_end_optionmenu = ctk.CTkOptionMenu(image_range_frame, values=["-"], width=80)
        self.image_range_end_optionmenu.bind("<Configure>", lambda event: self.clear_app_preset_status()) # 変更
        self.image_range_end_optionmenu.pack(side="left", padx=(5, 0))

        mic_frame = ctk.CTkFrame(setting_frame, fg_color="transparent")
        mic_frame.pack(fill="x", pady=5)
        ctk.CTkLabel(mic_frame, text="マイクデバイス:", width=100).pack(side="left", padx=(0, 5))
        self.mic_optionmenu = ctk.CTkOptionMenu(mic_frame, values=self.mic_device_names if self.mic_device_names else ["マイクなし"], command=lambda value: self.clear_app_preset_status()) # 変更
        self.mic_optionmenu.bind("<Configure>", lambda event: self.clear_app_preset_status()) # 変更
        self.mic_optionmenu.pack(side="left", fill="x", expand=True)
        
        # 音量閾値（下限）の入力欄をスライダーと数値入力のフレームに修正
        volume_min_frame = ctk.CTkFrame(setting_frame, fg_color="transparent")
        volume_min_frame.pack(fill="x", pady=5)
        
        button_frame_min = ctk.CTkFrame(volume_min_frame, fg_color="transparent")
        button_frame_min.pack(side="left", padx=(0, 5))
        ctk.CTkLabel(button_frame_min, text="音量閾値（下限）:", width=100).pack(side="left", padx=(0, 5))
        
        # ◀ボタン
        self.threshold_min_minus_button = ctk.CTkButton(
            button_frame_min, text="◀", width=30,
            command=lambda: self._change_threshold_value(self.threshold_min_slider, self.threshold_min_entry, -1)
        )
        self.threshold_min_minus_button.pack(side="left")
        self.threshold_min_minus_button.bind("<ButtonPress-1>", lambda event: self._start_continuous_change(self.threshold_min_slider, self.threshold_min_entry, -1))
        self.threshold_min_minus_button.bind("<ButtonRelease-1>", self._stop_continuous_change)
        
        # ▶ボタン
        self.threshold_min_plus_button = ctk.CTkButton(
            button_frame_min, text="▶", width=30,
            command=lambda: self._change_threshold_value(self.threshold_min_slider, self.threshold_min_entry, 1)
        )
        self.threshold_min_plus_button.pack(side="left", padx=(5, 0))
        self.threshold_min_plus_button.bind("<ButtonPress-1>", lambda event: self._start_continuous_change(self.threshold_min_slider, self.threshold_min_entry, 1))
        self.threshold_min_plus_button.bind("<ButtonRelease-1>", self._stop_continuous_change)
        
        self.threshold_min_slider = ctk.CTkSlider(volume_min_frame, from_=0, to=MAX_RMS_VALUE, command=self.update_volume_labels_from_slider)
        self.threshold_min_slider.pack(side="left", fill="x", expand=True)
        self.threshold_min_entry = ctk.CTkEntry(volume_min_frame, width=50)
        self.threshold_min_entry.insert(0, "0")
        self.threshold_min_entry.bind("<KeyRelease>", self.update_volume_labels_from_entry)
        self.threshold_min_entry.pack(side="left", padx=(5, 0))

        # 音量閾値（上限）の入力欄をスライダーと数値入力のフレームに修正
        volume_max_frame = ctk.CTkFrame(setting_frame, fg_color="transparent")
        volume_max_frame.pack(fill="x", pady=5)
        
        button_frame_max = ctk.CTkFrame(volume_max_frame, fg_color="transparent")
        button_frame_max.pack(side="left", padx=(0, 5))
        ctk.CTkLabel(button_frame_max, text="音量閾値（上限）:", width=100).pack(side="left", padx=(0, 5))
        
        # ◀ボタン
        self.threshold_max_minus_button = ctk.CTkButton(
            button_frame_max, text="◀", width=30,
            command=lambda: self._change_threshold_value(self.threshold_max_slider, self.threshold_max_entry, -1)
        )
        self.threshold_max_minus_button.pack(side="left")
        self.threshold_max_minus_button.bind("<ButtonPress-1>", lambda event: self._start_continuous_change(self.threshold_max_slider, self.threshold_max_entry, -1))
        self.threshold_max_minus_button.bind("<ButtonRelease-1>", self._stop_continuous_change)

        # ▶ボタン
        self.threshold_max_plus_button = ctk.CTkButton(
            button_frame_max, text="▶", width=30,
            command=lambda: self._change_threshold_value(self.threshold_max_slider, self.threshold_max_entry, 1)
        )
        self.threshold_max_plus_button.pack(side="left", padx=(5, 0))
        self.threshold_max_plus_button.bind("<ButtonPress-1>", lambda event: self._start_continuous_change(self.threshold_max_slider, self.threshold_max_entry, 1))
        self.threshold_max_plus_button.bind("<ButtonRelease-1>", self._stop_continuous_change)
        
        self.threshold_max_slider = ctk.CTkSlider(volume_max_frame, from_=0, to=MAX_RMS_VALUE, command=self.update_volume_labels_from_slider)
        self.threshold_max_slider.pack(side="left", fill="x", expand=True)
        self.threshold_max_entry = ctk.CTkEntry(volume_max_frame, width=50)
        self.threshold_max_entry.insert(0, "0")
        self.threshold_max_entry.bind("<KeyRelease>", self.update_volume_labels_from_entry)
        self.threshold_max_entry.pack(side="left", padx=(5, 0))
        
        self.threshold_min_slider.bind("<B1-Motion>", lambda event: self.clear_app_preset_status()) # 変更
        self.threshold_max_slider.bind("<B1-Motion>", lambda event: self.clear_app_preset_status()) # 変更

        self.threshold_min_slider.set(50)
        self.threshold_max_slider.set(500)
        self.threshold_min_entry.insert(0, "50")
        self.threshold_max_entry.insert(0, "500")

        # 閾値を設定し再起動ボタンを音量スライダーの下に移動
        # 修正: ボタンの状態を常に'normal'にする
        self.set_threshold_and_restart_button = ctk.CTkButton(setting_frame, text="閾値を設定し再起動", command=self.on_set_threshold_and_restart, state="normal")
        self.set_threshold_and_restart_button.pack(fill="x", pady=5, padx=10)
        
        # --- 修正箇所: 音量モニターのUIを再構築 ---
        self.volume_monitor_frame = ctk.CTkFrame(self, corner_radius=10)
        self.volume_monitor_frame.pack(fill="x", padx=10, pady=10)
        ctk.CTkLabel(self.volume_monitor_frame, text="音量モニター", font=ctk.CTkFont(size=14, weight="bold")).pack(pady=(5, 0))
        
        self.volume_progress_container = ctk.CTkFrame(self.volume_monitor_frame, height=20, fg_color="transparent")
        self.volume_progress_container.pack(fill="x", padx=10, pady=5)
        self.volume_progress_container.grid_columnconfigure(0, weight=1)
        self.volume_progress_container.grid_rowconfigure(0, weight=1)

        self.volume_progress = ctk.CTkProgressBar(self.volume_progress_container, orientation="horizontal", mode="determinate", height=10)
        self.volume_progress.pack(fill="x")
        self.volume_progress.set(0)

        # 閾値マーカーと数値ラベルのUIを修正
        self.min_threshold_marker = ctk.CTkFrame(self.volume_progress_container, width=2, height=10, fg_color="red")
        self.max_threshold_marker = ctk.CTkFrame(self.volume_progress_container, width=2, height=10, fg_color="green")
        
        # 閾値マーカーの数値ラベルを追加
        self.min_threshold_label = ctk.CTkLabel(self.volume_monitor_frame, text="0", text_color="red")
        self.max_threshold_label = ctk.CTkLabel(self.volume_monitor_frame, text="0", text_color="green")
        # --- 修正箇所ここまで ---
        
        # 実行ボタンの上の適用中プリセット表示
        applied_presets_frame = ctk.CTkFrame(self, fg_color="transparent")
        applied_presets_frame.pack(fill="x", padx=10, pady=(0, 5))
        self.obs_preset_label_bottom = ctk.CTkLabel(applied_presets_frame, textvariable=self.obs_preset_var, font=ctk.CTkFont(size=14))
        self.obs_preset_label_bottom.pack(fill="x", pady=(0, 2))
        self.app_preset_label_bottom = ctk.CTkLabel(applied_presets_frame, textvariable=self.app_preset_var, font=ctk.CTkFont(size=14))
        self.app_preset_label_bottom.pack(fill="x", pady=(2, 0))
        
        # 実行ボタンを再配置
        button_frame = ctk.CTkFrame(self, fg_color="transparent")
        button_frame.pack(fill="x", padx=10, pady=(0, 5))
        self.start_button = ctk.CTkButton(button_frame, text="▶ 開始", command=self.on_start)
        self.start_button.pack(side="left", expand=True, fill="x", padx=(0, 5))
        self.stop_button = ctk.CTkButton(button_frame, text="■ 停止", command=self.on_stop, state="disabled")
        self.stop_button.pack(side="left", expand=True, fill="x", padx=(5, 5))
        # 修正: ボタンの状態を常に'normal'にする
        self.restart_button = ctk.CTkButton(button_frame, text="↻ 再起動", command=self.on_restart, state="normal")
        self.restart_button.pack(side="left", expand=True, fill="x", padx=(5, 0))

        self.status_label = ctk.CTkLabel(self, text="準備完了", text_color="green", font=ctk.CTkFont(size=14, weight="bold"))
        self.status_label.pack(fill="x", pady=(0, 5))
        
        self.update_volume_labels_from_slider()
        # 修正: 閾値マーカーと数値ラベルの初期配置を調整
        self.after(100, self.update_threshold_markers)

    def add_entry_with_label(self, parent, label_text, default_value, command):
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.pack(fill="x", padx=10, pady=5)
        ctk.CTkLabel(frame, text=label_text, width=100).pack(side="left", padx=(0, 5))
        entry = ctk.CTkEntry(frame)
        entry.insert(0, default_value)
        entry.bind("<KeyRelease>", lambda event: command())
        entry.pack(side="left", fill="x", expand=True)
        return entry
        
    def load_auto_load_settings(self):
        try:
            with open(AUTO_LOAD_SETTINGS_FILE, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            return {"auto_load": False}
            
    def save_auto_load_settings(self):
        # auto_load_checkboxをauto_search_checkboxに名称変更
        settings = {"auto_load": self.auto_search_checkbox.get()}
        with open(AUTO_LOAD_SETTINGS_FILE, "w") as f:
            json.dump(settings, f)

    def load_theme_settings(self):
        try:
            with open(THEME_SETTINGS_FILE, "r") as f:
                settings = json.load(f)
                return settings.get("theme", "Dark")
        except FileNotFoundError:
            return "Dark"
            
    def save_theme_settings(self):
        with open(THEME_SETTINGS_FILE, "w") as f:
            json.dump({"theme": self.current_theme_name}, f)
            
    def change_appearance_mode(self):
        new_theme = "Light" if self.appearance_mode_switch.get() else "Dark"
        ctk.set_appearance_mode(new_theme)
        self.current_theme_name = new_theme
        self.current_theme_label.configure(text=f"（{new_theme}）")
        self.save_theme_settings()

    def update_preset_list(self):
        files = [f.replace(".json", "") for f in os.listdir(PRESET_FOLDER) if f.endswith(".json")]
        self.preset_optionmenu.configure(values=files if files else ["-"])
        
    def save_preset(self):
        preset_name = self.preset_name_entry.get().strip()
        if not preset_name or not re.match(r'^[a-zA-Z0-9_-]+$', preset_name):
            self.show_error("プリセット名には半角英数字、ハイフン、アンダースコアのみ使用可能です。")
            return
            
        file_path = os.path.join(PRESET_FOLDER, f"{preset_name}.json")
        
        # ファイルがすでに存在するかをチェックする
        if os.path.exists(file_path):
            # 存在する場合は上書き確認メッセージを表示
            response = messagebox.askyesno(
                "上書き確認",
                f"アプリ設定プリセット '{preset_name}' はすでに存在します。上書きしますか？"
            )
            # 「いいえ」が選択された場合は保存を中止
            if not response:
                return
        
        try:
            # 現在のGUI設定からデータを取得
            data = {
                "mic_device": self.mic_optionmenu.get(),
                "threshold_min": self.threshold_min_slider.get(),
                "threshold_max": self.threshold_max_slider.get(),
                "scene_name": self.scene_name_optionmenu.get(),
                "group_name": self.group_name_optionmenu.get(),
                "image_range_start": self.image_range_start_optionmenu.get(),
                "image_range_end": self.image_range_end_optionmenu.get()
            }
            
            # ファイルにデータを書き込む
            with open(file_path, "w") as f:
                json.dump(data, f, indent=4)
            
            # UIを更新
            self.update_preset_list()
            self.preset_name_entry.delete(0, ctk.END)
            self.status_label.configure(text=f"アプリ設定プリセット '{preset_name}' を保存しました。", text_color="green")
            self.app_current_preset_label.configure(text=f"適用中: {preset_name} (保存済)")
            self.app_preset_var.set(f"アプリ設定: {preset_name} (保存済)")
            self.is_app_preset_valid = True

        except Exception as e:
            self.show_error(f"プリセットの保存に失敗しました: {e}")
        
    def load_preset(self):
        preset_name = self.preset_optionmenu.get()
        if preset_name == "-":
            return
            
        file_path = os.path.join(PRESET_FOLDER, f"{preset_name}.json")
        try:
            with open(file_path, "r") as f:
                data = json.load(f)
                
                # シーン名とグループ名を保持
                scene_name_to_set = data.get("scene_name", "-")
                group_name_to_set = data.get("group_name", "-")

                # シーンをまず設定し、その後の非同期処理でグループと画像範囲を設定
                self.scene_name_optionmenu.set(scene_name_to_set)

                # 非同期でグループリストを更新し、完了後にグループと画像範囲を設定する
                self.after(100, self._load_app_preset_async_helper, data)

                self.mic_optionmenu.set(data.get("mic_device", "マイクなし"))
                self.threshold_min_slider.set(data.get("threshold_min", 0))
                self.threshold_max_slider.set(data.get("threshold_max", 0))
                self.update_volume_labels_from_slider()
        except FileNotFoundError:
            self.show_error(f"アプリ設定プリセット '{preset_name}' が見つかりませんでした。")
            return
        except Exception as e:
            self.show_error(f"アプリ設定プリセットの読み込みに失敗しました: {e}")
            return
        self.app_current_preset_label.configure(text=f"適用中: {preset_name} (保存済)")
        self.app_preset_var.set(f"アプリ設定: {preset_name} (保存済)")
        self.status_label.configure(text=f"アプリ設定プリセット '{preset_name}' を適用しました。", text_color="green")
        self.is_app_preset_valid = True
        self.preset_optionmenu.set("-") # 適用後に選択欄をリセット
        self.preset_name_entry.delete(0, ctk.END) # この行を追加
    
    def _load_app_preset_async_helper(self, data):
        """非同期でグループリストを更新した後、プリセットの値を設定するヘルパーメソッド"""
        # _update_group_list_asyncが完了するまで待機
        self.update_group_list_async(group_name_to_set=data.get("group_name"))
        
        # グループ名が設定されたら、画像範囲も設定
        self.after(200, self._update_image_range_on_group_change_with_preset, data.get("image_range_start"), data.get("image_range_end"))
        
    def _update_image_range_on_group_change_with_preset(self, preset_start, preset_end):
        global current_image_ids
        selected_scene = self.scene_name_optionmenu.get()
        selected_group = self.group_name_optionmenu.get()

        if selected_scene == "-" or selected_group == "-":
            return
        
        cache_key = (selected_scene, selected_group)
        if cache_key in self.cache_image_ids:
            cached_data = self.cache_image_ids[cache_key]
            current_image_ids = cached_data
            
            image_indices = sorted([int(re.sub(r'[^0-9]', '', name)) for name in current_image_ids.keys()])
            str_indices = [str(x) for x in image_indices]
            
            if str_indices:
                self.image_range_start_optionmenu.configure(values=str_indices)
                self.image_range_end_optionmenu.configure(values=str_indices)
                
                if preset_start in str_indices:
                    self.image_range_start_optionmenu.set(preset_start)
                else:
                    self.image_range_start_optionmenu.set(str_indices[0])
                    
                if preset_end in str_indices:
                    self.image_range_end_optionmenu.set(preset_end)
                else:
                    self.image_range_end_optionmenu.set(str_indices[-1])
            else:
                self.image_range_start_optionmenu.configure(values=["-"])
                self.image_range_end_optionmenu.configure(values=["-"])
                self.image_range_start_optionmenu.set("-")
                self.image_range_end_optionmenu.set("-")

            self.found_images_label.configure(text=f"見つかった画像: {len(cached_data)}個")
            self.status_label.configure(text="✅ 画像データがロードされました。", text_color="green")
        else:
            self.image_range_start_optionmenu.configure(values=["-"])
            self.image_range_end_optionmenu.configure(values=["-"])
            self.image_range_start_optionmenu.set("-")
            self.image_range_end_optionmenu.set("-")
            self.found_images_label.configure(text="見つかった画像: 0個")
            self.status_label.configure(text="画像が見つかりませんでした。検索ボタンを押してください。", text_color="red")
            current_image_ids = {} # グローバル変数をクリア
        
    def delete_preset(self):
        preset_name = self.preset_optionmenu.get()
        if preset_name == "-":
            return
            
        response = messagebox.askyesno(
            "削除確認",
            f"アプリ設定プリセット '{preset_name}' を本当に削除しますか？"
        )
        if not response:
            return
            
        file_path = os.path.join(PRESET_FOLDER, f"{preset_name}.json")
        try:
            os.remove(file_path)
            self.update_preset_list()
            if self.app_current_preset_label.cget("text") == f"適用中: {preset_name} (保存済)":
                self.clear_app_preset_name()
            self.status_label.configure(text=f"アプリ設定プリセット '{preset_name}' を削除しました。", text_color="green")
            self.preset_optionmenu.set("-")
        except FileNotFoundError:
            self.show_error(f"アプリ設定プリセット '{preset_name}' が見つかりませんでした。")
        except Exception as e:
            self.show_error(f"アプリ設定プリセットの削除に失敗しました: {e}")

    def update_obs_preset_list(self):
        files = [f.replace(".json", "") for f in os.listdir(OBS_PRESET_FOLDER) if f.endswith(".json")]
        self.obs_preset_optionmenu.configure(values=files if files else ["-"])

    def save_obs_preset(self):
        preset_name = self.obs_preset_name_entry.get().strip()
        if not preset_name or not re.match(r'^[a-zA-Z0-9_-]+$', preset_name):
            self.show_error("プリセット名には半角英数字、ハイフン、アンダースコアのみ使用可能です。")
            return

        file_path = os.path.join(OBS_PRESET_FOLDER, f"{preset_name}.json")

        # ファイルがすでに存在するかをチェックする
        if os.path.exists(file_path):
            # 存在する場合は上書き確認メッセージを表示
            response = messagebox.askyesno(
                "上書き確認",
                f"OBS接続プリセット '{preset_name}' はすでに存在します。上書きしますか？"
            )
            # 「いいえ」が選択された場合は保存を中止
            if not response:
                return

        data = {
            ...
        }
        with open(file_path, "w") as f:
            json.dump(data, f, indent=4)
        self.update_obs_preset_list()
        self.obs_preset_name_entry.delete(0, ctk.END)
        self.status_label.configure(text=f"OBS接続プリセット '{preset_name}' を保存しました。", text_color="green")
        self.obs_current_preset_label.configure(text=f"適用中: {preset_name} (保存済)")
        self.obs_preset_var.set(f"OBS接続: {preset_name} (保存済)")
        self.is_obs_preset_valid = True

    def load_obs_preset(self):
        preset_name = self.obs_preset_optionmenu.get()
        if preset_name == "-":
            return
            
        file_path = os.path.join(OBS_PRESET_FOLDER, f"{preset_name}.json")
        try:
            with open(file_path, "r") as f:
                data = json.load(f)
                self.obs_host_entry.delete(0, ctk.END)
                self.obs_host_entry.insert(0, data.get("host", "localhost"))
                self.obs_port_entry.delete(0, ctk.END)
                self.obs_port_entry.insert(0, data.get("port", "4455"))
                self.obs_password_entry.delete(0, ctk.END)
                self.obs_password_entry.insert(0, data.get("password", ""))

                # 適用したプリセットのシーン名とグループ名を表示
                try:
                    app_preset_path = os.path.join(PRESET_FOLDER, f"{preset_name}.json")
                    if os.path.exists(app_preset_path):
                        with open(app_preset_path, "r") as f:
                            app_data = json.load(f)
                            scene_name = app_data.get("scene_name", "なし")
                            group_name = app_data.get("group_name", "なし")
                            print(f"✅ プリセット適用: シーン名 '{scene_name}', グループ名 '{group_name}'")
                except Exception as e:
                    print(f"⚠ 関連するアプリ設定プリセットの読み込みに失敗しました: {e}")
                
                # シーンリストを更新
                self.update_scene_list()
                
                # 修正部分: 自動検索のタイミングを遅延させる
                if self.auto_search_checkbox.get():
                    self.after(500, self.start_find_all_sources_thread)

        except FileNotFoundError:
            self.show_error(f"OBS接続プリセット '{preset_name}' が見つかりませんでした。")
            return
        except Exception as e:
            self.show_error(f"OBS接続プリセットの読み込みに失敗しました: {e}")
            return
            
        self.obs_current_preset_label.configure(text=f"適用中: {preset_name} (保存済)")
        self.obs_preset_var.set(f"OBS接続: {preset_name} (保存済)")
        self.status_label.configure(text=f"OBS接続プリセット '{preset_name}' を適用しました。", text_color="green")
        self.is_obs_preset_valid = True
        self.obs_preset_optionmenu.set("-") # 適用後に選択欄をリセット
        
    def delete_obs_preset(self):
        preset_name = self.obs_preset_optionmenu.get()
        if preset_name == "-":
            return
            
        response = messagebox.askyesno(
            "削除確認",
            f"OBS接続プリセット '{preset_name}' を本当に削除しますか？"
        )
        if not response:
            return
            
        file_path = os.path.join(OBS_PRESET_FOLDER, f"{preset_name}.json")
        try:
            os.remove(file_path)
            self.update_obs_preset_list()
            if self.obs_current_preset_label.cget("text") == f"適用中: {preset_name} (保存済)":
                self.clear_obs_preset_name()
            self.status_label.configure(text=f"OBS接続プリセット '{preset_name}' を削除しました。", text_color="green")
            self.obs_preset_optionmenu.set("-")
        except FileNotFoundError:
            self.show_error(f"OBS接続プリセット '{preset_name}' が見つかりませんでした。")
        except Exception as e:
            self.show_error(f"OBS接続プリセットの削除に失敗しました: {e}")

    def clear_obs_preset_name(self):
        self.obs_current_preset_label.configure(text="適用中: なし")
        self.obs_preset_var.set("OBS接続: なし")
        self.is_obs_preset_valid = False

    def clear_app_preset_name(self):
        self.app_current_preset_label.configure(text="適用中: なし")
        self.app_preset_var.set("アプリ設定: なし")
        self.is_app_preset_valid = False

    def clear_app_preset_status(self):
        current_text = self.app_current_preset_label.cget("text")
        if "(保存済)" in current_text:
            new_text = current_text.replace(" (保存済)", "")
            self.app_current_preset_label.configure(text=new_text)
            self.app_preset_var.set(new_text.replace("適用中: ", "アプリ設定: "))

    def clear_group_and_image_info(self, value=None):
        global current_image_ids
        self.group_name_optionmenu.set("-")
        self.group_name_optionmenu.configure(values=["-"]) 
        self.update_group_list_async()
        self.found_images_label.configure(text="見つかった画像: 0個")
        self.image_range_start_optionmenu.configure(values=["-"])
        self.image_range_end_optionmenu.configure(values=["-"])
        self.image_range_start_optionmenu.set("-")
        self.image_range_end_optionmenu.set("-")
        self.status_label.configure(text="シーンとグループを選択してください。", text_color="orange")
        current_image_ids = {}

    def update_volume_labels_from_slider(self, value=None):
        min_val = int(self.threshold_min_slider.get())
        max_val = int(self.threshold_max_slider.get())
        
        self.threshold_min_entry.delete(0, ctk.END)
        self.threshold_min_entry.insert(0, str(min_val))
        
        self.threshold_max_entry.delete(0, ctk.END)
        self.threshold_max_entry.insert(0, str(max_val))
        
        self.min_threshold_label.configure(text=f"{min_val}")
        self.max_threshold_label.configure(text=f"{max_val}")
        
        self.clear_app_preset_status()
        self.update_threshold_markers()
        
    def update_volume_labels_from_entry(self, event=None):
        try:
            min_val = int(self.threshold_min_entry.get())
            max_val = int(self.threshold_max_entry.get())
            
            min_val = max(0, min(MAX_RMS_VALUE, min_val))
            max_val = max(0, min(MAX_RMS_VALUE, max_val))

            self.threshold_min_slider.set(min_val)
            self.threshold_max_slider.set(max_val)

            self.min_threshold_label.configure(text=f"{min_val}")
            self.max_threshold_label.configure(text=f"{max_val}")

            self.clear_app_preset_status()
            self.update_threshold_markers()
            
        except ValueError:
            # 入力が無効な場合は何もしない
            pass

    # 修正: 閾値マーカーと数値ラベルの配置を調整
    def update_threshold_markers(self):
        min_pos = self.threshold_min_slider.get() / MAX_RMS_VALUE
        max_pos = self.threshold_max_slider.get() / MAX_RMS_VALUE
        
        # マーカーを配置
        self.min_threshold_marker.place(relx=min_pos, rely=0.5, anchor=ctk.CENTER)
        self.max_threshold_marker.place(relx=max_pos, rely=0.5, anchor=ctk.CENTER)
        
        # 数値ラベルをバーの下に配置
        label_y = self.volume_progress_container.winfo_y() + self.volume_progress_container.winfo_height() + 5
        self.min_threshold_label.place(relx=min_pos, y=label_y, anchor=ctk.N)
        self.max_threshold_label.place(relx=max_pos, y=label_y, anchor=ctk.N)

    def show_error(self, message):
        self.status_label.configure(text=f"エラー: {message}", text_color="red")
        messagebox.showerror("エラー", message)
        
    def test_obs_connection(self):
        self.status_label.configure(text="接続テスト中...", text_color="orange")
        self.update_idletasks()

        obs_client_local = AsyncOBS(self.obs_host_entry.get(), int(self.obs_port_entry.get()), self.obs_password_entry.get())
        if obs_client_local.connect():
            self.status_label.configure(text="✅ OBSへの接続に成功しました。", text_color="green")
            
            # シーンとグループ名を取得してログ出力
            try:
                scenes = obs_client_local.get_scene_list()
                if scenes:
                    scene_name = scenes[0]
                    groups = obs_client_local.get_group_list_in_scene(scene_name)
                    group_name = groups[0] if groups else "なし"
                    print(f"✅ 成功: シーン名 '{scene_name}', グループ名 '{group_name}'")
                else:
                    print("⚠ 接続成功: シーンが見つかりませんでした。")
            except Exception as e:
                print(f"⚠ シーン/グループ情報の取得に失敗しました: {e}")
                
            obs_client_local.disconnect()
            
            # シーンリストを更新
            self.update_scene_list()

            if self.auto_search_checkbox.get(): # 変更
                # 修正部分: 自動検索のタイミングを遅延させる
                self.after(500, self.start_find_all_sources_thread)
        else:
            self.status_label.configure(text="❌ OBSへの接続に失敗しました。", text_color="red")
            
    def _update_scene_list_async(self):
        self.status_label.configure(text="シーンリスト更新中...", text_color="orange")
        self.update_idletasks()
        
        obs_client_local = AsyncOBS(self.obs_host_entry.get(), int(self.obs_port_entry.get()), self.obs_password_entry.get())
        if not obs_client_local.connect():
            self.after(0, lambda: self.show_error("OBSに接続できませんでした。設定を確認してください。"))
            return
        
        scenes = obs_client_local.get_scene_list()
        obs_client_local.disconnect()

        if scenes:
            self.after(0, lambda: self.scene_name_optionmenu.configure(values=scenes))
            self.after(0, lambda: self.scene_name_optionmenu.set(scenes[0]))
            self.after(0, self.update_group_list_async)
            self.after(0, lambda: self.status_label.configure(text="✅ シーンリストを更新しました。", text_color="green"))
        else:
            self.after(0, lambda: self.scene_name_optionmenu.configure(values=["-"]))
            self.after(0, lambda: self.scene_name_optionmenu.set("-"))
            self.after(0, lambda: self.group_name_optionmenu.configure(values=["-"]))
            self.after(0, lambda: self.group_name_optionmenu.set("-"))
            self.after(0, lambda: self.show_error("シーンが見つかりませんでした。"))
            
    def update_scene_list(self):
        threading.Thread(target=self._update_scene_list_async).start()
        
    def _update_group_list_async(self, value=None, group_name_to_set=None):
        selected_scene = self.scene_name_optionmenu.get()
        if selected_scene == "-" or not selected_scene:
            self.after(0, lambda: self.group_name_optionmenu.configure(values=["-"]))
            self.after(0, lambda: self.group_name_optionmenu.set("-"))
            return

        self.status_label.configure(text="グループリスト更新中...", text_color="orange")
        self.update_idletasks()
        
        obs_client_local = AsyncOBS(self.obs_host_entry.get(), int(self.obs_port_entry.get()), self.obs_password_entry.get())
        if not obs_client_local.connect():
            self.after(0, lambda: self.show_error("OBSに接続できませんでした。設定を確認してください。"))
            return

        groups = obs_client_local.get_group_list_in_scene(selected_scene)
        obs_client_local.disconnect()
        
        # 修正部分: キャッシュに画像がないグループを非表示にする
        visible_groups = []
        for group in groups:
            cache_key = (selected_scene, group)
            if cache_key in self.cache_image_ids:
                if len(self.cache_image_ids[cache_key]) > 0:
                    visible_groups.append(group)
            else:
                visible_groups.append(group)

        if visible_groups:
            self.after(0, lambda: self.group_name_optionmenu.configure(values=["-"] + visible_groups))
            # プリセットからグループ名が指定されていれば設定
            if group_name_to_set and group_name_to_set in visible_groups:
                self.after(0, lambda: self.group_name_optionmenu.set(group_name_to_set))
            else:
                self.after(0, lambda: self.group_name_optionmenu.set("-"))
            self.after(0, lambda: self.status_label.configure(text="✅ グループリストを更新しました。", text_color="green"))
            self.after(0, self._update_image_range_on_group_change)

        else:
            self.after(0, lambda: self.group_name_optionmenu.configure(values=["-"]))
            self.after(0, lambda: self.group_name_optionmenu.set("-"))
            self.after(0, lambda: self.status_label.configure(text="⚠ グループが見つかりませんでした。", text_color="red"))
            self.after(0, lambda: self.image_range_start_optionmenu.configure(values=["-"]))
            self.after(0, lambda: self.image_range_end_optionmenu.configure(values=["-"]))
            self.after(0, lambda: self.found_images_label.configure(text="見つかった画像: 0個"))
            
    def update_group_list_async(self, value=None, group_name_to_set=None):
        threading.Thread(target=self._update_group_list_async, args=(value, group_name_to_set)).start()
        
    def _update_image_range_on_group_change(self, value=None):
        global current_image_ids
        selected_scene = self.scene_name_optionmenu.get()
        selected_group = self.group_name_optionmenu.get()
        
        if selected_scene == "-" or selected_group == "-":
            self.image_range_start_optionmenu.configure(values=["-"])
            self.image_range_end_optionmenu.configure(values=["-"])
            self.image_range_start_optionmenu.set("-")
            self.image_range_end_optionmenu.set("-")
            self.found_images_label.configure(text="見つかった画像: 0個")
            self.status_label.configure(text="シーンとグループを選択してください。", text_color="orange")
            current_image_ids = {}
            return
            
        cache_key = (selected_scene, selected_group)
        if cache_key in self.cache_image_ids:
            print(f"✅ キャッシュから画像データをロードします: {cache_key}")
            cached_data = self.cache_image_ids[cache_key]
            current_image_ids = cached_data
            
            # プリセットから設定された値があるか確認
            preset_file_path = os.path.join(PRESET_FOLDER, f"{self.app_preset_var.get().replace('アプリ設定: ', '').replace(' (保存済)', '')}.json")
            
            preset_start_range = "-"
            preset_end_range = "-"
            try:
                if os.path.exists(preset_file_path):
                    with open(preset_file_path, "r") as f:
                        data = json.load(f)
                        if data.get("scene_name") == selected_scene and data.get("group_name") == selected_group:
                            preset_start_range = data.get("image_range_start")
                            preset_end_range = data.get("image_range_end")
            except Exception:
                pass
            
            # 画像範囲の選択肢と値を更新
            image_indices = sorted([int(re.sub(r'[^0-9]', '', name)) for name in current_image_ids.keys()])
            str_indices = [str(x) for x in image_indices]
            
            if str_indices:
                self.image_range_start_optionmenu.configure(values=str_indices)
                self.image_range_end_optionmenu.configure(values=str_indices)
                
                # プリセットから読み込んだ値があれば設定
                if preset_start_range in str_indices:
                    self.image_range_start_optionmenu.set(preset_start_range)
                else:
                    self.image_range_start_optionmenu.set(str_indices[0])
                    
                if preset_end_range in str_indices:
                    self.image_range_end_optionmenu.set(preset_end_range)
                else:
                    self.image_range_end_optionmenu.set(str_indices[-1])
            else:
                self.image_range_start_optionmenu.configure(values=["-"])
                self.image_range_end_optionmenu.configure(values=["-"])
                self.image_range_start_optionmenu.set("-")
                self.image_range_end_optionmenu.set("-")

            self.found_images_label.configure(text=f"見つかった画像: {len(cached_data)}個")
            self.status_label.configure(text="✅ 画像データがロードされました。", text_color="green")
            
        else:
            print(f"⚠ キャッシュに画像データがありません: {cache_key}")
            self.image_range_start_optionmenu.configure(values=["-"])
            self.image_range_end_optionmenu.configure(values=["-"])
            self.image_range_start_optionmenu.set("-")
            self.image_range_end_optionmenu.set("-")
            self.found_images_label.configure(text="見つかった画像: 0個")
            self.status_label.configure(text="画像が見つかりませんでした。検索ボタンを押してください。", text_color="red")
            current_image_ids = {} # グローバル変数をクリア
            
    def start_find_all_sources_thread(self):
        self.is_searching = True # 検索中フラグを立てる
        self.load_preset_button.configure(state="disabled") # プリセット適用ボタンを無効化
        self.delete_preset_button.configure(state="disabled") # プリセット削除ボタンを無効化
        threading.Thread(target=self._find_all_sources_async).start()

    def _find_all_sources_async(self):
        global current_image_ids
        
        self.status_label.configure(text="全シーン・グループの画像ソースを検索中...", text_color="orange")
        self.update_idletasks()
        
        obs_client_local = AsyncOBS(self.obs_host_entry.get(), int(self.obs_port_entry.get()), self.obs_password_entry.get())
        if not obs_client_local.connect():
            self.after(0, self.on_search_complete, 0)
            self.after(0, lambda: self.show_error("OBSに接続できませんでした。"))
            return

        all_scenes = obs_client_local.get_scene_list()
        if not all_scenes:
            obs_client_local.disconnect()
            self.after(0, self.on_search_complete, 0)
            self.after(0, lambda: self.status_label.configure(text="⚠ シーンが見つかりませんでした。", text_color="red"))
            return

        total_found_count = 0
        
        try:
            for scene_name in all_scenes:
                groups = obs_client_local.get_group_list_in_scene(scene_name)
                
                for group_name in groups:
                    found_ids_for_group = {}
                    for i in range(1, MAX_IMAGE_COUNT + 1):
                        source_name = f"{i}"
                        source_id = obs_client_local.get_scene_item_id(group_name, source_name)
                        if source_id is not None:
                            found_ids_for_group[source_name] = source_id
                            total_found_count += 1
                    
                    # 修正部分: 画像が見つからない場合もキャッシュに残す
                    self.cache_image_ids[(scene_name, group_name)] = found_ids_for_group
        except Exception as e:
            print(f"検索中にエラーが発生しました: {e}")
            self.after(0, self.on_search_complete, 0)
            self.after(0, lambda: self.show_error(f"検索中にエラーが発生しました: {e}"))
        finally:
            obs_client_local.disconnect()
            self.after(0, self.update_group_list_async)
            self.after(0, self.on_search_complete, total_found_count)

    def start_find_sources_in_scene_thread(self):
        self.is_searching = True # 検索中フラグを立てる
        self.load_preset_button.configure(state="disabled") # プリセット適用ボタンを無効化
        self.delete_preset_button.configure(state="disabled") # プリセット削除ボタンを無効化
        threading.Thread(target=self._find_sources_in_scene_async).start()

    def _find_sources_in_scene_async(self):
        selected_scene = self.scene_name_optionmenu.get()
        if selected_scene == "-":
            self.after(0, self.on_search_complete, 0)
            self.after(0, lambda: self.show_error("シーンを選択してください。"))
            return
            
        self.status_label.configure(text=f"シーン '{selected_scene}' 内の画像ソースを検索中...", text_color="orange")
        self.update_idletasks()
        
        obs_client_local = AsyncOBS(self.obs_host_entry.get(), int(self.obs_port_entry.get()), self.obs_password_entry.get())
        if not obs_client_local.connect():
            self.after(0, self.on_search_complete, 0)
            self.after(0, lambda: self.show_error("OBSに接続できませんでした。"))
            return
        
        total_found_count = 0
        
        try:
            groups = obs_client_local.get_group_list_in_scene(selected_scene)
            for group_name in groups:
                found_ids_for_group = {}
                for i in range(1, MAX_IMAGE_COUNT + 1):
                    source_name = f"{i}.png"
                    source_id = obs_client_local.get_scene_item_id(group_name, source_name)
                    if source_id is not None:
                        found_ids_for_group[source_name] = source_id
                        total_found_count += 1
                
                self.cache_image_ids[(selected_scene, group_name)] = found_ids_for_group
        except Exception as e:
            print(f"検索中にエラーが発生しました: {e}")
            self.after(0, self.on_search_complete, 0)
            self.after(0, lambda: self.show_error(f"検索中にエラーが発生しました: {e}"))
        finally:
            obs_client_local.disconnect()
            self.after(0, self.update_group_list_async)
            self.after(0, self.on_search_complete, total_found_count)

    def start_find_sources_in_group_thread(self):
        self.is_searching = True # 検索中フラグを立てる
        self.load_preset_button.configure(state="disabled") # プリセット適用ボタンを無効化
        self.delete_preset_button.configure(state="disabled") # プリセット削除ボタンを無効化
        threading.Thread(target=self._find_sources_in_group_async).start()

    def _find_sources_in_group_async(self):
        selected_scene = self.scene_name_optionmenu.get()
        selected_group = self.group_name_optionmenu.get()
        if selected_scene == "-" or selected_group == "-":
            self.after(0, self.on_search_complete, 0)
            self.after(0, lambda: self.show_error("シーンとグループを選択してください。"))
            return
            
        self.status_label.configure(text=f"グループ '{selected_group}' 内の画像ソースを検索中...", text_color="orange")
        self.update_idletasks()
        
        obs_client_local = AsyncOBS(self.obs_host_entry.get(), int(self.obs_port_entry.get()), self.obs_password_entry.get())
        if not obs_client_local.connect():
            self.after(0, self.on_search_complete, 0)
            self.after(0, lambda: self.show_error("OBSに接続できませんでした。"))
            return
            
        found_ids_for_group = {}
        try:
            for i in range(1, MAX_IMAGE_COUNT + 1):
                source_name = f"{i}.png"
                source_id = obs_client_local.get_scene_item_id(selected_group, source_name)
                if source_id is not None:
                    found_ids_for_group[source_name] = source_id
        except Exception as e:
            print(f"検索中にエラーが発生しました: {e}")
            self.after(0, self.on_search_complete, 0)
            self.after(0, lambda: self.show_error(f"検索中にエラーが発生しました: {e}"))
        finally:
            obs_client_local.disconnect()
            self.cache_image_ids[(selected_scene, selected_group)] = found_ids_for_group
            self.after(0, self._update_image_range_on_group_change)
            self.after(0, self.on_search_complete, len(found_ids_for_group))

    def on_search_complete(self, count):
        self.is_searching = False # 検索中フラグをリセット
        self.load_preset_button.configure(state="normal") # プリセット適用ボタンを有効化
        self.delete_preset_button.configure(state="normal") # プリセット削除ボタンを有効化

        if count > 0:
            self.status_label.configure(text=f"✅ {count}個の画像ソースが見つかりました。", text_color="green")
            self.found_images_label.configure(text=f"見つかった画像: {count}個")
            
            # 選択中のシーン・グループの画像リストを更新
            selected_scene = self.scene_name_optionmenu.get()
            selected_group = self.group_name_optionmenu.get()
            cache_key = (selected_scene, selected_group)

            global current_image_ids
            if cache_key in self.cache_image_ids:
                cached_data = self.cache_image_ids[cache_key]
                current_image_ids = cached_data
                image_indices = sorted([int(re.sub(r'[^0-9]', '', name)) for name in current_image_ids.keys()])
                if image_indices:
                    str_indices = [str(x) for x in image_indices]
                    self.image_range_start_optionmenu.configure(values=str_indices)
                    self.image_range_start_optionmenu.set(str_indices[0])
                    self.image_range_end_optionmenu.configure(values=str_indices)
                    self.image_range_end_optionmenu.set(str_indices[-1])
            else:
                current_image_ids = {}
                self.image_range_start_optionmenu.configure(values=["-"])
                self.image_range_end_optionmenu.configure(values=["-"])
                self.status_label.configure(text="⚠ 選択されたグループに画像ソースがありません。", text_color="red")

        else:
            self.status_label.configure(text="⚠ 画像ソースが見つかりませんでした。ソース名とグループ名を確認してください。", text_color="red")
            self.found_images_label.configure(text="見つかった画像: 0個")
            self.image_range_start_optionmenu.configure(values=["-"])
            self.image_range_end_optionmenu.configure(values=["-"])
        self.update_idletasks()
        self.clear_app_preset_status() # 変更

    def on_start(self):
        global current_threshold_min, current_threshold_max, selected_mic_index, current_scene_name, current_group_name
        
        # 修正部分: 選択されたシーンとグループのキャッシュから画像IDを再ロードする
        selected_scene = self.scene_name_optionmenu.get()
        selected_group = self.group_name_optionmenu.get()
        cache_key = (selected_scene, selected_group)
        global current_image_ids
        current_image_ids = self.cache_image_ids.get(cache_key, {})

        if len(current_image_ids) == 0:
            self.show_error("画像ソースが検出されていません。「検索」ボタンを押してください。")
            return
        
        mic_name = self.mic_optionmenu.get()
        mic_info = next((dev for dev in self.mic_devices if dev["name"] == mic_name), None)
        if not mic_info:
            self.show_error("マイクデバイスが選択されていません。")
            return
            
        try:
            start_index = int(self.image_range_start_optionmenu.get())
            end_index = int(self.image_range_end_optionmenu.get())
            if start_index > end_index:
                self.show_error("画像範囲の開始番号は終了番号より小さく設定してください。")
                return
        except ValueError:
            self.show_error("画像範囲が正しく選択されていません。")
            return

        current_scene_name = self.scene_name_optionmenu.get()
        current_group_name = self.group_name_optionmenu.get()
        current_threshold_min = self.threshold_min_slider.get()
        current_threshold_max = self.threshold_max_slider.get()
        selected_mic_index = mic_info["index"]

        if not current_scene_name or current_scene_name == "-" or not current_group_name or current_group_name == "-":
            self.show_error("シーン名とグループ名を指定してください。")
            return

        if current_threshold_min >= current_threshold_max:
            self.show_error("音量閾値の下限は上限より小さく設定してください。")
            return
            
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        # 修正: 再起動ボタンの状態変更を削除
        self.status_label.configure(text="▶ 音量監視中...", text_color="blue")
        start_audio_thread(self)

    def on_stop(self):
        global obs_client
        
        stop_audio_thread()
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        # 修正: 再起動ボタンの状態変更を削除
        self.status_label.configure(text="■ 停止しました", text_color="green")
        
        if obs_client:
            obs_client.disconnect()
            obs_client = None

    def on_restart(self):
        self.on_stop()
        self.after(500, self.on_start) # 停止処理が完了するまで少し待つ

    def on_set_threshold_and_restart(self):
        global current_threshold_min, current_threshold_max
        try:
            min_val = int(self.threshold_min_entry.get())
            max_val = int(self.threshold_max_entry.get())
            if min_val >= max_val:
                self.show_error("音量閾値の下限は上限より小さく設定してください。")
                return
            current_threshold_min = min_val
            current_threshold_max = max_val
            
            self.threshold_min_slider.set(min_val)
            self.threshold_max_slider.set(max_val)
            self.update_threshold_markers()
            
            self.on_restart()
        except ValueError:
            self.show_error("閾値には数値を入力してください。")

    def on_closing(self):
        global run_audio_thread
        if run_audio_thread:
            stop_audio_thread()
        self.destroy()

    def update_volume_monitor(self):
        try:
            while not audio_data_queue.empty():
                rms = audio_data_queue.get_nowait()
                normalized_volume = min(1.0, rms / MAX_RMS_VALUE)
                self.volume_progress.set(normalized_volume)
                
                # update_threshold_markersでまとめて更新するため、ここでは呼び出しのみにする
                self.update_threshold_markers()
                
                progress_color = "gray"
                if rms >= self.threshold_max_slider.get():
                    progress_color = "#3a7ebf"
                elif rms >= self.threshold_min_slider.get():
                    progress_color = "green"
                
                self.volume_progress.configure(progress_color=progress_color)
        except queue.Empty:
            pass
        finally:
            self.after(50, self.update_volume_monitor)

    def _change_threshold_value(self, slider_obj, entry_obj, change_amount):
        """スライダーとエントリーの値を変更するヘルパーメソッド"""
        current_value = slider_obj.get()
        new_value = max(0, min(MAX_RMS_VALUE, current_value + change_amount))
        slider_obj.set(new_value)
        entry_obj.delete(0, ctk.END)
        entry_obj.insert(0, str(int(new_value)))
        self.update_volume_labels_from_slider()
        self.clear_app_preset_status()

    def _start_continuous_change(self, slider_obj, entry_obj, change_amount):
        """長押しで継続的な変更を開始する"""
        self.change_is_active = True
        # 100msごとに値を変更
        self.after(100, self._continue_change, slider_obj, entry_obj, change_amount)

    def _continue_change(self, slider_obj, entry_obj, change_amount):
        """連続的な変更を続ける"""
        if self.change_is_active:
            self._change_threshold_value(slider_obj, entry_obj, change_amount)
            # 100msごとに次の変更をスケジュール
            self.after(100, self._continue_change, slider_obj, entry_obj, change_amount)

    def _stop_continuous_change(self, event=None):
        """ボタンが離されたときに変更を停止する"""
        self.change_is_active = False

    def show_manual(self):
        file_path = "OBS生声ゆっくり_取扱説明書.txt"

        # ファイルが存在しない場合はエラーメッセージを表示して終了
        if not os.path.exists(file_path):
            messagebox.showerror("エラー", f"ファイルが見つかりません: \n{file_path}")
            return

        # 新しいウィンドウを作成
        manual_window = ctk.CTkToplevel(self)
        manual_window.title("取扱説明書")
        manual_window.geometry("600x400")
        manual_window.after(10, manual_window.lift) # ウィンドウを前面に表示

        # テキストボックスを配置
        manual_textbox = ctk.CTkTextbox(manual_window, wrap="word") # wrap="word"で単語の途中で改行しないようにする
        manual_textbox.pack(fill="both", expand=True, padx=10, pady=10)

        try:
            # ファイルを読み込んでテキストボックスに挿入
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            manual_textbox.insert("1.0", content)
        except Exception as e:
            manual_textbox.insert("1.0", f"ファイルの読み込み中にエラーが発生しました。\n\nエラー詳細: {e}")
        
        # テキストボックスを読み取り専用にする
        manual_textbox.configure(state="disabled")

if __name__ == "__main__":
    app = App()
    app.mainloop()
