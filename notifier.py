"""Show a notification on this device using the operating system's own notification system.

Nothing extra is installed and nothing leaves the device:
  - Windows: a Windows notification (toast), created through PowerShell
  - macOS:   osascript
  - Linux:   notify-send
"""

import base64
import os
import subprocess
import sys

# PowerShell script that shows a Windows toast. The text is passed in environment
# variables, so quotes or other special characters in it can never break the script.
WINDOWS_TOAST_SCRIPT = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText04)
$lines = @($env:PV_TITLE, $env:PV_LINE1, $env:PV_LINE2)
$texts = $template.GetElementsByTagName("text")
for ($i = 0; $i -lt 3; $i++) { $texts.Item($i).AppendChild($template.CreateTextNode($lines[$i])) > $null }
$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""


def send_system_notification(title, line1, line2=""):
    """Show a notification. Returns None if it was shown, otherwise an error message."""
    try:
        if sys.platform == "win32":
            encoded = base64.b64encode(WINDOWS_TOAST_SCRIPT.encode("utf-16-le")).decode()
            env = dict(os.environ, PV_TITLE=title, PV_LINE1=line1, PV_LINE2=line2)
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                env=env, capture_output=True, text=True, timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        elif sys.platform == "darwin":
            text = f"{line1} {line2}".replace('"', "'")
            result = subprocess.run(
                ["osascript", "-e", f'display notification "{text}" with title "{title}"'],
                capture_output=True, text=True, timeout=20)
        else:
            result = subprocess.run(["notify-send", title, f"{line1}\n{line2}"],
                                    capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as error:
        return str(error)

    if result.returncode != 0:
        return (result.stderr or result.stdout or f"exit code {result.returncode}").strip()[:300]
    return None
