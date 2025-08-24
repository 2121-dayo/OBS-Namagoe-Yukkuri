' Create WScript.Shell object
Set objShell = CreateObject("WScript.Shell")

' Check if Python is installed
result = objShell.Run("cmd /c python --version", 0, True)

If result <> 0 Then
    objShell.Popup "Python is not installed. Please download and install it from the official website.", 0, "Python Not Found", 48 + 0
    WScript.Quit
End If

' List of plugins to check
Dim plugins(3)
plugins(0) = "numpy"
plugins(1) = "pyaudio"
plugins(2) = "customtkinter"
plugins(3) = "obswebsocket"

' Check each plugin one by one
For Each plugin In plugins
    result = objShell.Run("cmd /c python -c ""import " & plugin & """", 0, True)
    If result <> 0 Then
        objShell.Popup plugin & " is not installed. Please run 'pip install " & plugin & "' to install it.", 0, "Plugin Not Found", 48 + 0
        WScript.Quit
    End If
Next

' If all plugins are present, run the main script
objShell.Run "cmd /c python OBSNamagoeYukkuriScript.py", 0, True
