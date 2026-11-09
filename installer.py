# -*- coding: utf-8 -*-
"""yisi-定时工具 安装程序

选择安装位置后，把打包好的「yisi-定时工具.exe」和默认配置文件解压到目标目录，
可选创建桌面快捷方式和开机启动快捷方式。配置始终跟随 exe 所在目录。
"""
import ctypes
import os
import shutil
import subprocess
import sys

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QProgressBar, QPushButton, QVBoxLayout,
)

APP_NAME = 'yisi-定时工具'
APP_DIR_NAME = '倚肆工具'
PUBLISHER = '倚肆工具'
APP_VERSION = '1.2.0'
APP_EXE = APP_NAME + '.exe'
CONFIG_FILES = ['reminder_options.json', 'reminders.json', 'categories.json']
# 改名前的程序文件名，安装时顺手清掉，避免新旧两个 exe 并存
LEGACY_EXE_NAMES = ('下班提醒.exe',)
# 改名前的快捷方式，安装时一并清理
LEGACY_SHORTCUT_NAMES = ('下班提醒.lnk', '下班打卡提醒.lnk')
CREATE_NO_WINDOW = 0x08000000


# -------------------- 路径与小工具 --------------------
def payload_dir():
    """打包后从 _MEIPASS\\payload 取随包文件，源码运行时取脚本同级的 payload"""
    base = getattr(sys, '_MEIPASS', None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, 'payload')


def resolve_install_dir(chosen):
    """安装目录统一落在所选位置下的「倚肆工具」子文件夹里，卸载时整个文件夹一起删"""
    chosen = os.path.abspath(chosen)
    if os.path.basename(os.path.normpath(chosen)) == APP_DIR_NAME:
        return chosen
    return os.path.join(chosen, APP_DIR_NAME)


def shell_folder(csidl):
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
    return buf.value


def desktop_dir():
    # 测试时可覆盖
    return os.environ.get('XJ_DESKTOP_DIR') or shell_folder(0x10)  # CSIDL_DESKTOPDIRECTORY


def startup_dir():
    return os.environ.get('XJ_STARTUP_DIR') or shell_folder(0x07)  # CSIDL_STARTUP


def default_install_dir():
    local = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    return os.path.join(local, 'Programs')


def create_shortcut(lnk_path, target, workdir):
    """用 WScript.Shell 创建 .lnk，避免依赖 pywin32"""
    os.makedirs(os.path.dirname(lnk_path), exist_ok=True)
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut('{lnk_path}'); "
        f"$s.TargetPath = '{target}'; "
        f"$s.WorkingDirectory = '{workdir}'; "
        f"$s.IconLocation = '{target},0'; "
        f"$s.Description = '{APP_NAME}：上下班打卡提醒'; "
        "$s.Save()"
    )
    subprocess.run(
        ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script],
        check=True, capture_output=True, creationflags=CREATE_NO_WINDOW,
    )


def is_running(exe_path):
    """exe 被占用即视为正在运行"""
    if not os.path.exists(exe_path):
        return False
    try:
        with open(exe_path, 'ab'):
            pass
        return False
    except OSError:
        return True


UNINSTALL_REG_PATH = r'Software\Microsoft\Windows\CurrentVersion\Uninstall' + '\\' + APP_NAME
LEGACY_UNINSTALL_REG_PATHS = (
    r'Software\Microsoft\Windows\CurrentVersion\Uninstall\下班提醒',
)


def register_uninstall(exe_path, target_dir):
    """写入「应用和功能」里的卸载入口，UninstallString 指向 下班提醒.exe --uninstall"""
    import winreg
    key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, UNINSTALL_REG_PATH, 0,
                             winreg.KEY_WRITE)
    values = {
        'DisplayName': APP_NAME,
        'DisplayVersion': APP_VERSION,
        'Publisher': PUBLISHER,
        'DisplayIcon': exe_path,
        'InstallLocation': target_dir,
        'UninstallString': f'"{exe_path}" --uninstall',
        'QuietUninstallString': f'"{exe_path}" --uninstall --yes',
        'NoModify': 1,
        'NoRepair': 1,
    }
    try:
        for name, value in values.items():
            kind = winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ
            winreg.SetValueEx(key, name, 0, kind, value)
    finally:
        winreg.CloseKey(key)


def unregister_uninstall():
    import winreg
    for path in (UNINSTALL_REG_PATH,) + LEGACY_UNINSTALL_REG_PATHS:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except Exception:
            continue


def install(target_dir, desktop_shortcut=True, startup_shortcut=False, on_step=None):
    """把程序和默认配置装到 target_dir；已有配置不覆盖，避免升级丢设置"""
    target_dir = resolve_install_dir(target_dir)
    src = payload_dir()
    steps = []

    def report(text):
        steps.append(text)
        if on_step:
            on_step(text)

    report('创建安装目录…')
    os.makedirs(target_dir, exist_ok=True)

    exe_src = os.path.join(src, APP_EXE)
    exe_dst = os.path.join(target_dir, APP_EXE)
    if not os.path.exists(exe_src):
        raise RuntimeError(f'安装包里没有找到 {APP_EXE}')
    if is_running(exe_dst):
        raise RuntimeError('程序正在运行，请先右键托盘图标退出后再安装')

    report('复制程序文件…')
    shutil.copy2(exe_src, exe_dst)

    for name in LEGACY_EXE_NAMES:
        old = os.path.join(target_dir, name)
        if os.path.exists(old):
            try:
                os.remove(old)
            except OSError:
                pass

    report('写入默认配置…')
    for name in CONFIG_FILES:
        s = os.path.join(src, name)
        d = os.path.join(target_dir, name)
        if os.path.exists(s) and not os.path.exists(d):
            shutil.copy2(s, d)

    for folder in (desktop_dir(), startup_dir()):
        for name in LEGACY_SHORTCUT_NAMES:
            old = os.path.join(folder, name)
            if os.path.exists(old):
                try:
                    os.remove(old)
                except OSError:
                    pass

    if desktop_shortcut:
        report('创建桌面快捷方式…')
        create_shortcut(os.path.join(desktop_dir(), f'{APP_NAME}.lnk'), exe_dst, target_dir)

    if startup_shortcut:
        report('设置开机自动启动…')
        create_shortcut(os.path.join(startup_dir(), f'{APP_NAME}.lnk'), exe_dst, target_dir)

    report('注册卸载信息…')
    register_uninstall(exe_dst, target_dir)

    report('安装完成')
    return exe_dst


# -------------------- 界面 --------------------
STYLE = """
QDialog { background: #f7f7f5; }
QLabel#title { color: #1b1b1b; }
QLabel#desc { color: #6b6b6b; }
QLabel#hint { color: #8a8a8a; font-size: 12px; }
QLabel#status { color: #8a8a8a; }
QLineEdit {
    background: #ffffff; border: 1px solid #dcdcd6; border-radius: 6px;
    padding: 7px 10px; color: #1b1b1b;
}
QPushButton {
    background: #ffffff; border: 1px solid #dcdcd6; border-radius: 6px;
    padding: 7px 18px; color: #1b1b1b;
}
QPushButton:hover { border-color: #cf4813; color: #cf4813; }
QPushButton#primary {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #f97316, stop:1 #cf4813);
    border: none; border-radius: 8px; padding: 11px 30px; color: #ffffff;
}
QPushButton#primary:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #ff8f31, stop:1 #e2541b);
}
QPushButton#primary:disabled { background: #d8d8d2; color: #ffffff; }
QCheckBox { color: #3d3d3d; spacing: 8px; }
QProgressBar {
    background: #e8e8e2; border: none; border-radius: 4px; height: 8px;
}
QProgressBar::chunk { border-radius: 4px; background: #cf4813; }
"""


class Installer(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f'{APP_NAME} 安装程序')
        self.setFixedWidth(560)
        self.setStyleSheet(STYLE)
        self._exe = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 22)
        root.setSpacing(14)

        title = QLabel(f'安装 {APP_NAME}')
        title.setObjectName('title')
        title_font = QFont('Microsoft YaHei UI')
        title_font.setPixelSize(22)
        title_font.setWeight(QFont.DemiBold)
        title.setFont(title_font)
        root.addWidget(title)

        desc = QLabel('上下班打卡提醒：开机提醒、关机前拦截、定时提醒与定时关机。\n配置文件会放在程序所在目录，便于整体搬迁或备份。')
        desc.setObjectName('desc')
        desc.setWordWrap(True)
        root.addWidget(desc)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(default_install_dir())
        self.path_edit.textChanged.connect(self._update_hint)
        browse = QPushButton('浏览…')
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(self._choose_dir)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        root.addLayout(path_row)

        self.hint = QLabel('')
        self.hint.setObjectName('hint')
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)
        self._update_hint()

        self.desktop_check = QCheckBox('创建桌面快捷方式')
        self.desktop_check.setChecked(True)
        root.addWidget(self.desktop_check)

        self.startup_check = QCheckBox('开机自动启动（加入启动文件夹）')
        root.addWidget(self.startup_check)

        self.run_check = QCheckBox('安装完成后立即运行')
        self.run_check.setChecked(True)
        root.addWidget(self.run_check)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedHeight(8)
        self.progress.hide()
        root.addWidget(self.progress)

        self.status = QLabel('')
        self.status.setObjectName('status')
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self.cancel_btn = QPushButton('取消')
        self.cancel_btn.clicked.connect(self.reject)
        self.install_btn = QPushButton('开始安装')
        self.install_btn.setObjectName('primary')
        self.install_btn.setCursor(Qt.PointingHandCursor)
        self.install_btn.clicked.connect(self._install)
        button_row.addWidget(self.cancel_btn)
        button_row.addWidget(self.install_btn)
        root.addLayout(button_row)

    def _update_hint(self):
        chosen = self.path_edit.text().strip().strip('"')
        if not chosen:
            self.hint.setText('')
            return
        final = resolve_install_dir(chosen)
        self.hint.setText(f'实际安装到：{final}\n卸载时只删除这个文件夹，所选位置里的其他文件不受影响。')

    def _choose_dir(self):
        path = QFileDialog.getExistingDirectory(self, '选择安装位置', self.path_edit.text() or default_install_dir())
        if path:
            self.path_edit.setText(path)

    def _step(self, text):
        self.status.setText(text)
        QApplication.processEvents()

    def _install(self):
        chosen = self.path_edit.text().strip().strip('"')
        if not chosen:
            QMessageBox.warning(self, '安装位置', '请先选择安装位置')
            return
        target = resolve_install_dir(os.path.abspath(chosen))
        if os.path.exists(target):
            reply = QMessageBox.question(
                self, '目录已存在',
                f'{target}\n目录已存在，将覆盖同名的程序文件（已有配置会保留）。是否继续？',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if reply != QMessageBox.Yes:
                return

        self.install_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.progress.show()
        self.setCursor(Qt.WaitCursor)
        try:
            exe = install(
                target,
                desktop_shortcut=self.desktop_check.isChecked(),
                startup_shortcut=self.startup_check.isChecked(),
                on_step=self._step,
            )
            self._exe = exe
        except Exception as e:
            self.progress.hide()
            self.install_btn.setEnabled(True)
            self.cancel_btn.setEnabled(True)
            self.unsetCursor()
            QMessageBox.critical(self, '安装失败', str(e))
            return

        self.unsetCursor()
        self.progress.hide()
        self.install_btn.setText('完成')
        self.install_btn.setEnabled(True)
        self.install_btn.clicked.disconnect()
        self.install_btn.clicked.connect(self._finish)
        self.cancel_btn.hide()
        self.status.setText(f'已安装到：{target}')
        self.path_edit.setReadOnly(True)

    def _finish(self):
        if self.run_check.isChecked() and self._exe and os.path.exists(self._exe):
            try:
                subprocess.Popen([self._exe], cwd=os.path.dirname(self._exe))
            except Exception as e:
                QMessageBox.warning(self, '启动失败', str(e))
        self.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    if '--silent' in sys.argv:
        # 静默安装，供命令行/自动化使用：--silent --dir=<安装目录> [--desktop]
        target = default_install_dir()
        for arg in sys.argv:
            if arg.startswith('--dir='):
                target = arg.split('=', 1)[1].strip('"')
        install(target, desktop_shortcut='--desktop' in sys.argv, startup_shortcut=False)
        return 0
    dialog = Installer()
    dialog.show()
    return app.exec_()


if __name__ == '__main__':
    sys.exit(main())
