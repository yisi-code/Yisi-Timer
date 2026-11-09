import sys
import os
import json
import shutil
import copy
import re
import math
import random
import time
from datetime import datetime, timedelta

from PyQt5.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QAction, QWidget, QLabel,
    QInputDialog, QMessageBox, QVBoxLayout, QHBoxLayout, QPushButton,
    QFrame, QProgressBar, QGraphicsDropShadowEffect, QDialog, QLineEdit,
    QPlainTextEdit, QComboBox, QSpinBox, QCheckBox, QFormLayout, QToolButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QScrollArea, QColorDialog,
    QStyledItemDelegate, QListWidget, QListWidgetItem, QAbstractItemView, QRadioButton
)
from PyQt5.QtCore import (QTimer, Qt, pyqtSignal, QPointF, QRectF, QEvent, QSize, QObject,
                          QRect, QPoint, QThread, QMimeData, QUrl)
from PyQt5.QtGui import (
    QFont, QIcon, QPixmap, QPainter, QColor, QFontMetrics, QPainterPath,
    QLinearGradient, QRadialGradient, QBrush, QPen, QPalette, QDrag, QRegion, QDesktopServices
)
import tempfile
import subprocess
import hashlib
import urllib.request
import ctypes
import ctypes.wintypes

try:
    from zhdate import ZhDate
except Exception:  # 缺依赖时退化为只显示公历
    ZhDate = None

# -------------------- 配置路径（始终在程序同目录） --------------------
def get_config_path():
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, "reminder_times.json")

CONFIG_FILE = get_config_path()        # 旧版定时提醒配置，仅在迁移时读取
OPTIONS_FILE = os.path.join(os.path.dirname(CONFIG_FILE), "reminder_options.json")
RULES_FILE = os.path.join(os.path.dirname(CONFIG_FILE), "reminders.json")
CATEGORIES_FILE = os.path.join(os.path.dirname(CONFIG_FILE), "categories.json")

# 规则 = 触发器 + 动作。开关机提醒、上下班打卡只是预置好的规则（PRESETS），
# 用户也可以自己组装触发器与动作，托盘菜单完全由 rules 生成，不再写死。
rules = []
categories = []            # 首页分类，规则用 category 字段引用分类 id
options = {}               # 全局设置：时间显示格式、主题外观
rule_timer = None          # 所有规则共用一个定时器，取最近一次触发
reminded_dates = {}        # 同一分钟内不重复触发
_reminder_window = None    # 当前显示的提醒窗口
_reminder_text = ""
retry_timer = None         # 5 秒重试定时器
snooze_timer = None        # 稍后提醒定时器
paused_until = None        # 暂停定时提醒到这个时间点之前（关机拦截不受影响）

# -------------------- 全局设置（reminder_options.json） --------------------
DEFAULT_OPTIONS = {
    "display": {
        "date_format": "yyyy-MM-dd",   # yyyy-MM-dd / yy-MM-dd / yyyy年M月d日
        "time_format": "HH:mm:ss",      # HH:mm:ss / HH:mm
        "show_weekday": True,
        "show_lunar": True,
        "show_ganzhi": True,
        "show_zodiac": True,
        "font_scale": 1.0,          # 界面字号倍率：0.9 / 1.0 / 1.15 / 1.3
    },
    "shutdown": {
        "auto_release_seconds": 30,     # 关机拦截弹窗多少秒后自动放行
    },
    "snooze_minutes": 5,
    "language": "zh",                # 新规则默认的稍后提醒间隔
    "theme": {
        "name": "night",                # night / dawn / dusk
        "accent": "",                   # 自定义强调色（#rrggbb），留空用主题自带
    },
}

# 新规则的默认值（延迟本身按规则保存，这里只作为新建时的初始值）
DEFAULT_AUTO_RELEASE_SECONDS = 30
DEFAULT_SNOOZE_MINUTES = 5

FONT_SCALE_CHOICES = [("小 90%", 0.9), ("标准 100%", 1.0),
                      ("大 115%", 1.15), ("特大 130%", 1.3)]

DATE_FORMAT_CHOICES = [
    ("yyyy-MM-dd", "2026-11-03"),
    ("yyyy年M月d日", "2026年11月3日"),
    ("yyyy/MM/dd", "2026/11/03"),
    ("MM-dd", "11-03"),
    ("M月d日", "11月3日"),
]

TIME_FORMAT_CHOICES = [
    ("HH:mm:ss", "21:04:59"),
    ("HH:mm", "21:04"),
    ("hh:mm:ss", "09:04:59"),
]


def fill_format_combo(combo, choices, current):
    """格式下拉：显示样例 + 实际格式串，用户不用记 yyyy/MM 这些写法"""
    combo.clear()
    for value, sample in choices:
        combo.addItem(f"{sample}    {value}", value)
    index = combo.findData(current)
    if index < 0:
        combo.insertItem(0, str(current), current)
        index = 0
    combo.setCurrentIndex(index)

# 触发器拆成两段，互相独立组合：
#   触发类别 kind   —— 定时（到点提醒/关机）、开机（开机后提醒）
#   时间     repeat —— 每日、工作日、自定义（勾选星期）
TRIGGER_SCHEDULE = 'schedule'
TRIGGER_BOOT = 'boot'
ACTION_REMIND = 'remind'
ACTION_SHUTDOWN = 'shutdown'

TRIGGER_KIND_LABELS = [
    (TRIGGER_SCHEDULE, "定时"),
    (TRIGGER_BOOT, "开机"),
]
REPEAT_DAILY = 'daily'
REPEAT_WORKDAY = 'workday'
REPEAT_CUSTOM = 'custom'
REPEAT_LABELS = [
    (REPEAT_DAILY, "每日"),
    (REPEAT_WORKDAY, "工作日"),
    (REPEAT_CUSTOM, "自定义"),
]
# 旧版触发器类型，只在迁移老配置时认得
LEGACY_TRIGGER_TYPES = ('daily', 'workday', 'weekly')
ACTION_LABELS = [
    (ACTION_REMIND, "弹窗提醒"),
    (ACTION_SHUTDOWN, "关机"),
]
WEEKDAY_CN = '一二三四五六日'
WORKDAYS = [0, 1, 2, 3, 4]

# 首页默认分类；删除分类时里面的规则会挪到「未分类」，不会丢
CAT_COMMUTE = 'commute'
CAT_POWER = 'power'
CAT_SHUTDOWN = 'shutdown'
CAT_OTHER = 'other'
# 新装不再预置分类：分类与提醒都交给用户自己建，升级时不动老配置
DEFAULT_CATEGORIES = []
CATEGORY_OTHER_NAME = "未分类"


def default_category_for(trigger, action):
    """没指定分类时按动作/触发器推断；分类都没建过就统一放「未分类」"""
    if (action or {}).get('type') == ACTION_SHUTDOWN:
        preferred = CAT_SHUTDOWN
    elif trigger_kind(trigger) == TRIGGER_BOOT:
        preferred = CAT_POWER
    else:
        preferred = CAT_COMMUTE
    if preferred in category_ids():
        return preferred
    # 没有内置同名分类时，落到用户自己建的第一个分类；一个分类都没有就进「未分类」
    for item in categories:
        return item['id']
    return CAT_OTHER


def category_ids():
    return {item.get('id') for item in categories}


def category_name(cid):
    for item in categories:
        if item.get('id') == cid:
            return item.get('name', '')
    return CATEGORY_OTHER_NAME


def category_of_rule(rule):
    """规则所属分类 id；分类被删除后归到「未分类」"""
    cid = rule.get('category') or default_category_for(rule.get('trigger'), rule.get('action'))
    return cid if cid in category_ids() else CAT_OTHER


def load_categories():
    """读取首页分类；首次运行是空列表（不再预置），之后以文件为准"""
    global categories
    if not os.path.exists(CATEGORIES_FILE):
        categories = [dict(item) for item in DEFAULT_CATEGORIES]
        save_categories()
        return
    categories = []
    try:
        with open(CATEGORIES_FILE, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
        if isinstance(data, list):
            seen = set()
            for item in data:
                if not isinstance(item, dict):
                    continue
                cid = str(item.get('id') or '').strip()
                name = str(item.get('name') or '').strip()
                if not cid or not name or cid in seen:
                    continue
                seen.add(cid)
                categories.append({"id": cid, "name": name})
    except Exception as e:
        print("读取 categories.json 失败，按空分类处理：", e)


def save_categories():
    try:
        with open(CATEGORIES_FILE, 'w', encoding='utf-8') as f:
            json.dump(categories, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("保存 categories.json 失败：", e)

GAN = '甲乙丙丁戊己庚辛壬癸'
ZHI = '子丑寅卯辰巳午未申酉戌亥'
ZODIAC = '鼠牛虎兔龙蛇马羊猴鸡狗猪'
GAN_PINYIN = ('Jia', 'Yi', 'Bing', 'Ding', 'Wu', 'Ji', 'Geng', 'Xin', 'Ren', 'Gui')
ZHI_PINYIN = ('Zi', 'Chou', 'Yin', 'Mao', 'Chen', 'Si', 'Wu', 'Wei', 'Shen', 'You', 'Xu', 'Hai')
ZODIAC_EN = ('Rat', 'Ox', 'Tiger', 'Rabbit', 'Dragon', 'Snake',
             'Horse', 'Goat', 'Monkey', 'Rooster', 'Dog', 'Pig')
LUNAR_MONTHS = ['正月', '二月', '三月', '四月', '五月', '六月',
                '七月', '八月', '九月', '十月', '冬月', '腊月']


def make_rule(name, trigger, action, enabled=True, preset=None, category=None):
    """统一构造规则对象，避免各处手写字段遗漏"""
    rule = {
        "id": f"{int(time.time() * 1000):x}{random.randint(100, 999)}",
        "name": name,
        "enabled": bool(enabled),
        "category": category or default_category_for(trigger, action),
        "trigger": trigger,
        "action": action,
    }
    if preset:
        rule["preset"] = preset
    return rule


def remind_action(title, content, hint='', badge='提醒', button_text='', snooze=None):
    return {
        "type": ACTION_REMIND,
        "title": title,
        "content": content,
        "hint": hint,
        "badge": badge,
        "button_text": button_text,
        "snooze_minutes": snooze,
    }


def normalize_days(days):
    """星期的合法化：只留 0..6 的整数并去重排序"""
    out = []
    for item in days or []:
        try:
            day = int(item)
        except Exception:
            continue
        if 0 <= day <= 6 and day not in out:
            out.append(day)
    return sorted(out)


def repeat_for_days(days):
    """星期集合 → 时间口径：空=每日，周一到周五=工作日，其它=自定义"""
    days = normalize_days(days)
    if not days:
        return REPEAT_DAILY
    if days == WORKDAYS:
        return REPEAT_WORKDAY
    return REPEAT_CUSTOM


def trigger_kind(trigger):
    """触发类别：schedule（定时）/ boot（开机）；兼容旧版 type 字段"""
    trig = trigger or {}
    kind = trig.get('kind')
    if kind in (TRIGGER_SCHEDULE, TRIGGER_BOOT):
        return kind
    return TRIGGER_BOOT if trig.get('type') == TRIGGER_BOOT else TRIGGER_SCHEDULE


def trigger_repeat(trigger):
    """时间口径：daily / workday / custom；兼容旧版 daily、workday、weekly"""
    trig = trigger or {}
    repeat = trig.get('repeat')
    if repeat in (REPEAT_DAILY, REPEAT_WORKDAY, REPEAT_CUSTOM):
        return repeat
    legacy = trig.get('type')
    if legacy == 'workday':
        return REPEAT_WORKDAY
    if legacy == 'weekly':
        return repeat_for_days(trig.get('weekdays')) or REPEAT_DAILY
    return repeat_for_days(trig.get('weekdays'))


def make_trigger(kind, repeat=REPEAT_DAILY, weekdays=None, times=None, window=None):
    """统一构造触发器：类别 + 时间口径，定时类才带具体时间点"""
    trigger = {'kind': kind, 'repeat': repeat}
    if repeat == REPEAT_CUSTOM:
        trigger['weekdays'] = normalize_days(weekdays) or list(WORKDAYS)
    if kind == TRIGGER_SCHEDULE:
        trigger['times'] = list(times or [])
    if window:
        trigger['window'] = dict(window)
    return trigger


def schedule_trigger(times, weekdays=None):
    """定时触发：时间点 + 星期口径"""
    days = normalize_days(weekdays)
    repeat = repeat_for_days(days)
    return make_trigger(TRIGGER_SCHEDULE, repeat,
                        days if repeat == REPEAT_CUSTOM else None, times)


def boot_trigger(weekdays=None):
    """开机触发：只决定哪几天生效"""
    days = normalize_days(weekdays)
    repeat = repeat_for_days(days)
    return make_trigger(TRIGGER_BOOT, repeat, days if repeat == REPEAT_CUSTOM else None)


# 预设：托盘「添加规则」里的快捷模板，生成后与自定义规则完全一致，可再编辑
PRESETS = [
    {
        "key": "morning",
        "label": "上班打卡提醒",
        "make": lambda: make_rule(
            "上班打卡", schedule_trigger(["08:00"], WORKDAYS),
            remind_action("上班打卡", "请打开手机企微", "请确认手机企微已完成打卡",
                          "上班提醒", "已打开企微", 5),
            preset="morning"),
    },
    {
        "key": "evening",
        "label": "下班打卡提醒",
        "make": lambda: make_rule(
            "下班打卡", schedule_trigger(["17:30"], WORKDAYS),
            remind_action("下班打卡", "请确认手机企微已打卡", "",
                          "下班提醒", "已打卡", 5),
            preset="evening"),
    },
    {
        "key": "boot",
        "label": "开机提醒",
        "make": lambda: make_rule(
            "开机提醒", boot_trigger(),
            remind_action("上班打卡", "请打开手机企微", "请确认手机企微已完成打卡",
                          "上班提醒", "已打开企微"),
            preset="boot"),
    },
    {
        "key": "shutdown_power",
        "label": "定时关机",
        "make": lambda: make_rule(
            "定时关机", schedule_trigger(["18:30"], WORKDAYS),
            {"type": ACTION_SHUTDOWN}, preset="shutdown_power"),
    },
]

def load_options():
    """读取全局设置，缺失项用默认值补齐"""
    global options
    options = json.loads(json.dumps(DEFAULT_OPTIONS))
    if os.path.exists(OPTIONS_FILE):
        try:
            with open(OPTIONS_FILE, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
            for key, value in data.items():
                if key not in options:
                    continue
                if isinstance(options[key], dict) and isinstance(value, dict):
                    # 只接受默认结构里认识的键，旧版遗留字段（text/enabled 等）丢弃
                    for sub_key, sub_value in value.items():
                        if sub_key in options[key]:
                            options[key][sub_key] = sub_value
                else:
                    options[key] = value
        except Exception as e:
            print("读取 reminder_options.json 失败，使用默认配置：", e)

def save_options():
    try:
        with open(OPTIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(options, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("保存 reminder_options.json 失败：", e)


def save_rules():
    try:
        with open(RULES_FILE, 'w', encoding='utf-8') as f:
            json.dump(rules, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("保存 reminders.json 失败：", e)

def _migrate_rule_defaults(items):
    """老规则的延迟存在全局设置里，这里补到规则自身，之后各规则独立"""
    legacy_seconds = int((options.get('shutdown') or {}).get('auto_release_seconds')
                         or DEFAULT_AUTO_RELEASE_SECONDS)
    for rule in items:
        action = rule.get('action') or {}
        if action.get('type') == ACTION_SHUTDOWN and not action.get('auto_release_seconds'):
            action['auto_release_seconds'] = legacy_seconds
    return items


def _migrate_shutdown_rules(items):
    """「关机前拦截」已移除：老规则改成对应时段结束时间的定时提醒，不直接丢配置"""
    for rule in items:
        trig = rule.get('trigger') or {}
        if trig.get('type') != 'shutdown':
            continue
        end = (trig.get('window') or {}).get('end') or '18:30'
        rule['trigger'] = schedule_trigger([end])
        rule['name'] = rule.get('name') or '下班打卡'
        rule.pop('preset', None)
        # 变成普通定时提醒后放进「上下班提醒」，不再挂「开关机拦截」
        if rule.get('category') == CAT_POWER:
            rule['category'] = CAT_COMMUTE
    return items


def _migrate_triggers(items):
    """旧版触发器（daily/workday/weekly/boot）升级成「类别 + 时间」两段式，设置一律保留"""
    for rule in items:
        trig = rule.get('trigger') or {}
        if trig.get('kind') in (TRIGGER_SCHEDULE, TRIGGER_BOOT) and 'type' not in trig:
            continue
        kind = trigger_kind(trig)
        repeat = trigger_repeat(trig)
        times = trig.get('times') if isinstance(trig.get('times'), list) else []
        days = trig.get('weekdays') if repeat == REPEAT_CUSTOM else None
        window = trig.get('window') or None
        rule['trigger'] = make_trigger(
            kind, repeat, days,
            times if kind == TRIGGER_SCHEDULE else None, window)
    return items


def load_rules():
    """读取规则列表；首次运行或从旧版升级时，把旧配置迁移成规则"""
    global rules
    rules = []
    if os.path.exists(RULES_FILE):
        try:
            with open(RULES_FILE, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
            if isinstance(data, list):
                rules = [r for r in data if isinstance(r, dict) and r.get('trigger') and r.get('action')]
                # 老版本规则没有分类：按动作/触发器补一个默认分类
                for rule in rules:
                    if not rule.get('category'):
                        rule['category'] = default_category_for(rule.get('trigger'), rule.get('action'))
                _migrate_shutdown_rules(rules)
                _migrate_triggers(rules)
                _migrate_rule_defaults(rules)
                save_rules()
        except Exception as e:
            print("读取 reminders.json 失败：", e)
    if not rules:
        if migrate_legacy():
            save_rules()
        else:
            save_rules()

def _split_legacy_text(text, fallback_title):
    lines = [ln.strip() for ln in str(text or '').split('\n') if ln.strip()]
    if not lines:
        return fallback_title, ''
    return lines[0], '\n'.join(lines[1:])

def legacy_title_content(cfg):
    """旧配置只有 text（可能多行），标题取首行、内容取其余行"""
    if cfg.get('title') or cfg.get('content'):
        return cfg.get('title', ''), cfg.get('content', '')
    return _split_legacy_text(cfg.get('text'), '提醒')

def migrate_legacy():
    """把旧版 reminder_times.json / reminder_options.json 转成统一规则"""
    global rules
    legacy_options = {}
    if os.path.exists(OPTIONS_FILE):
        try:
            with open(OPTIONS_FILE, 'r', encoding='utf-8-sig') as f:
                legacy_options = json.load(f)
        except Exception:
            legacy_options = {}

    migrated = []
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8-sig') as f:
                old_times = json.load(f)
        except Exception:
            old_times = []
        for item in old_times if isinstance(old_times, list) else []:
            try:
                hh, mm = int(item['hour']), int(item['minute'])
            except Exception:
                continue
            title, content = _split_legacy_text(item.get('text'), '提醒')
            migrated.append(make_rule(
                title, schedule_trigger([f"{hh:02d}:{mm:02d}"]),
                remind_action(title, content, '', '定时提醒', '',
                              options.get('snooze_minutes', 5)),
                enabled=item.get('enabled', True)))

    boot = legacy_options.get('boot') or {}
    if boot.get('text') or boot.get('title'):
        title, content = legacy_title_content(boot)
        migrated.append(make_rule(
            '开机提醒', boot_trigger(),
            remind_action(title, content, "请确认手机企微已完成打卡", "上班提醒", "已打开企微"),
            enabled=boot.get('enabled', True), preset='boot'))

    schedule = legacy_options.get('shutdown_schedule') or {}
    for item in schedule.get('times') or []:
        try:
            hh, mm = int(item['hour']), int(item['minute'])
        except Exception:
            continue
        weekdays = WORKDAYS if schedule.get('workdays_only', True) else []
        migrated.append(make_rule(
            f"{hh:02d}:{mm:02d} 定时关机", schedule_trigger([f"{hh:02d}:{mm:02d}"], weekdays),
            {"type": ACTION_SHUTDOWN}, enabled=item.get('enabled', True),
            preset='shutdown_power'))

    if not migrated:
        return False
    rules = migrated
    save_options()   # 写回后旧键（boot/shutdown/shutdown_schedule）自然消失
    return True

# -------------------- 开机自启 --------------------
def get_startup_folder():
    try:
        CSIDL_STARTUP = 7
        buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
        ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_STARTUP, None, 0, buf)
        return buf.value
    except:
        return os.path.join(os.environ['APPDATA'],
                            r'Microsoft\Windows\Start Menu\Programs\Startup')

APP_NAME = "yisi-定时工具"
SHORTCUT_NAME = APP_NAME + ".lnk"
# 改名前的快捷方式名，读状态与卸载时都要认得，避免留下残余
LEGACY_SHORTCUT_NAMES = ("下班提醒.lnk", "下班打卡提醒.lnk")

def get_shortcut_path():
    return os.path.join(get_startup_folder(), SHORTCUT_NAME)

def _legacy_shortcut_paths():
    return [os.path.join(get_startup_folder(), name) for name in LEGACY_SHORTCUT_NAMES]

def is_startup_enabled():
    return os.path.exists(get_shortcut_path()) or any(
        os.path.exists(path) for path in _legacy_shortcut_paths())

def create_shortcut():
    shortcut_path = get_shortcut_path()
    if getattr(sys, 'frozen', False):
        target_path = sys.executable
        arguments = ''
        working_dir = os.path.dirname(sys.executable)
    else:
        python_exe = sys.executable
        if python_exe.endswith('python.exe'):
            pythonw_exe = python_exe.replace('python.exe', 'pythonw.exe')
            if os.path.exists(pythonw_exe):
                python_exe = pythonw_exe
        target_path = python_exe
        script_path = os.path.abspath(__file__)
        if script_path.endswith('.py'):
            script_path_pyw = script_path[:-3] + '.pyw'
            if os.path.exists(script_path_pyw):
                script_path = script_path_pyw
        arguments = f'"{script_path}"'
        working_dir = os.path.dirname(script_path)

    ps_script = f'''
$WshShell = New-Object -comObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("{shortcut_path}")
$Shortcut.TargetPath = "{target_path}"
$Shortcut.Arguments = '{arguments}'
$Shortcut.WorkingDirectory = "{working_dir}"
$Shortcut.WindowStyle = 7
$Shortcut.Save()
'''
    # 失败时抛给调用方：这个函数会在后台线程里跑，不能直接弹框
    subprocess.run(['powershell', '-Command', ps_script], check=True,
                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    return True

def delete_shortcut():
    for path in [get_shortcut_path()] + _legacy_shortcut_paths():
        if os.path.exists(path):
            os.remove(path)
    return True

def _run_startup_change(enabled):
    """（可能耗时）真正建/删启动快捷方式，返回 (ok, error)；供后台线程调用"""
    try:
        if enabled:
            if not is_startup_enabled():
                create_shortcut()
        else:
            if is_startup_enabled():
                delete_shortcut()
        return True, ''
    except Exception as exc:
        return False, str(exc)


def set_startup_enabled(enabled, notify=False):
    """同步版本：会阻塞调用线程，只给没有界面的场合用"""
    ok, error = _run_startup_change(enabled)
    if notify:
        if ok:
            notify(None, "已启用" if enabled else "已取消",
                   "已开启开机自启" if enabled else "已取消开机自启")
        else:
            notify(None, "错误", f"设置开机自启失败：{error}")
    update_menu()
    return ok


class _StartupTask(QThread):
    """后台开关开机自启：PowerShell 建快捷方式要一两秒，放主线程会卡界面"""

    done = pyqtSignal(bool, str)

    def __init__(self, enabled):
        super().__init__()
        self._enabled = enabled

    def run(self):
        ok, error = _run_startup_change(self._enabled)
        self.done.emit(ok, error)


_startup_tasks = []


def set_startup_enabled_async(enabled, callback=None):
    """在后台线程里开关开机自启，callback(ok, error) 回到主线程执行"""
    if _startup_tasks:
        return None          # 已有一次在跑，忽略重复点击
    task = _StartupTask(enabled)
    _startup_tasks.append(task)

    def finished(ok, error):
        if task in _startup_tasks:
            _startup_tasks.remove(task)
        update_menu()
        if callback is not None:
            callback(ok, error)

    task.done.connect(finished)
    task.start()
    return task


def toggle_startup():
    """托盘菜单：后台切换开机自启"""
    target = not is_startup_enabled()
    set_startup_enabled_async(target, lambda ok, error: None if ok else notify(
        None, "设置失败", f"无法修改开机自启：{error}"))


def startup_dependent_rules():
    """开机提醒与关机拦截都要求程序在运行，开机自启没开时这些规则不会生效"""
    dependents = []
    for rule in rules:
        if not rule.get('enabled', True):
            continue
        if trigger_kind(rule.get('trigger')) == TRIGGER_BOOT:
            dependents.append(rule)
    return dependents


# -------------------- 卸载：清掉自启、快捷方式、配置与注册表 --------------------
UNINSTALL_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Uninstall" + "\\" + APP_NAME
# 改名前的注册表项，卸载时一并清掉
LEGACY_UNINSTALL_REG_PATHS = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall\下班提醒",
)

def app_dir():
    return os.path.dirname(CONFIG_FILE)

def _desktop_dirs():
    """用户桌面与公共桌面；CSIDL 失败时退回 ~/Desktop"""
    dirs = []
    try:
        buf = ctypes.create_unicode_buffer(260)
        # CSIDL_DESKTOPDIRECTORY = 0x0010
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0:
            dirs.append(buf.value)
    except Exception:
        pass
    for path in (os.path.join(os.path.expanduser('~'), 'Desktop'),
                 os.path.join(os.environ.get('PUBLIC', r'C:\Users\Public'), 'Desktop')):
        if os.path.isdir(path):
            dirs.append(path)
    return dirs

def shortcut_files():
    """开机启动和桌面上可能存在的快捷方式（含旧名字），去重后返回"""
    names = (SHORTCUT_NAME,) + LEGACY_SHORTCUT_NAMES
    paths = [get_shortcut_path()] + _legacy_shortcut_paths()
    for folder in _desktop_dirs():
        paths.extend(os.path.join(folder, name) for name in names)
    seen, result = set(), []
    for path in paths:
        key = path.lower()
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result

def config_files():
    base = app_dir()
    return [CONFIG_FILE, OPTIONS_FILE, RULES_FILE, CATEGORIES_FILE,
            os.path.join(base, 'shutdown_guard.log')]

def remove_registry_entry():
    try:
        import winreg
    except Exception:
        return
    for path in (UNINSTALL_REG_PATH,) + LEGACY_UNINSTALL_REG_PATHS:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except Exception:
            continue


def register_uninstall_info():
    """在「应用和功能」里登记卸载入口：直接双击 exe 运行也能登记，挪了目录下次启动会自动更正"""
    if not getattr(sys, 'frozen', False):
        return
    try:
        import winreg
    except Exception:
        return
    exe = sys.executable
    folder = app_dir()
    values = {
        'DisplayName': APP_NAME,
        'DisplayVersion': APP_VERSION,
        'Publisher': '倚肆工具',
        'DisplayIcon': exe,
        'InstallLocation': folder,
        'UninstallString': f'"{exe}" --uninstall',
        'QuietUninstallString': f'"{exe}" --uninstall --yes',
        'NoModify': 1,
        'NoRepair': 1,
    }
    try:
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, UNINSTALL_REG_PATH, 0,
                                 winreg.KEY_WRITE)
        try:
            for name, value in values.items():
                kind = winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ
                winreg.SetValueEx(key, name, 0, kind, value)
        finally:
            winreg.CloseKey(key)
    except Exception:
        pass

def schedule_self_delete():
    """运行中的 exe 删不掉：留一个临时批处理，等本进程退出后再删 exe 和程序目录

    只用 cmd 内建命令：控制台程序（ping / timeout / choice）在无控制台的子进程里
    会弹「应用程序无法正常启动 0xc0000142」，所以延时用自旋循环，重试到自己消失为止。
    """
    if not getattr(sys, 'frozen', False):
        return False
    exe = sys.executable
    folder = app_dir()
    script = (
        '@echo off\r\n'
        'set /a tries=0\r\n'
        ':retry\r\n'
        f'del /f /q "{exe}" 2>nul\r\n'
        f'rmdir /s /q "{folder}" 2>nul\r\n'
        f'if not exist "{exe}" if not exist "{folder}" exit /b 0\r\n'
        'set /a tries+=1\r\n'
        'if %tries% geq 120 exit /b 1\r\n'
        'for /L %%i in (1,1,2000000) do @rem\r\n'
        'goto retry\r\n'
    )
    bat = os.path.join(tempfile.gettempdir(), 'uninstall_xjtx.bat')
    try:
        with open(bat, 'w', encoding='mbcs', errors='ignore') as handle:
            handle.write(script)
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        subprocess.Popen(['cmd', '/c', bat], creationflags=flags, close_fds=True)
        return True
    except Exception:
        return False

def run_uninstall(parent=None, assume_yes=False):
    """确认后删除全部痕迹；assume_yes 供静默卸载/命令行使用"""
    frozen = bool(getattr(sys, 'frozen', False))
    targets = shortcut_files() + config_files()
    existing = [path for path in targets if os.path.exists(path)]

    lines = ["将删除以下内容：", ""]
    if existing:
        lines.extend(f"· {path}" for path in existing)
    else:
        lines.append("· （没有找到快捷方式与配置文件）")
    if frozen:
        lines.append(f"· 程序目录：{app_dir()}")
    lines.append("· 注册表中的卸载信息")
    lines.extend(["", "删除后不可恢复。"])

    if not assume_yes:
        if not ask_confirm(parent, f"卸载 {APP_NAME}", "\n".join(lines),
                           ok_text="卸载", cancel_text="取消", danger=True):
            return False
    for timer in (rule_timer, retry_timer, snooze_timer):
        if timer is not None:
            timer.stop()

    failures = []
    remove_registry_entry()
    for path in targets:
        if not os.path.exists(path):
            continue
        try:
            os.remove(path)
        except Exception as exc:
            failures.append(f"{path}：{exc}")
    if frozen and not schedule_self_delete():
        failures.append(f"{app_dir()}：（无法安排自动删除，请手动删除该目录）")

    if not assume_yes:
        if failures:
            notify(parent, "卸载未完全完成", "以下内容没能删除：\n" + "\n".join(failures))
        else:
            notify(parent, "已卸载", "配置、快捷方式、注册表信息已清空。" +
                   ("\n程序文件会在退出后自动删除。" if frozen else ""))
    return True

# -------------------- 关于与自动更新 --------------------
APP_VERSION = "1.2.0"
APP_REPO_GITHUB = "yisi-code/Yisi-Timer"
APP_REPO_GITEE = "yisi-code/yisi-timer"  # Gitee 路径只允许字母数字，显示名仍是「yisi-定时工具」
APP_HOME_URL = f"https://github.com/{APP_REPO_GITHUB}"
APP_MIRROR_URL = f"https://gitee.com/{APP_REPO_GITEE}"
APP_RELEASES_URL = APP_HOME_URL + "/releases/latest"
APP_LICENSE = "PolyForm Noncommercial 1.0.0"
APP_AUTHOR = "倚肆"
APP_WECHAT = "m1751250104"
APP_QQ = "1751250104"
APP_MAIL = "xiejing0520@petalmail.com"
APP_DIR_NAME = "倚肆工具"

UPDATE_HEADER = "yisi-timer update v1"
UPDATE_ASSET = f"安装 {APP_NAME}.exe"
UPDATE_MANIFEST = UPDATE_ASSET + ".sig"
# P-256 公钥（X、Y 各 32 字节）；私钥只在维护者本机，不进仓库
UPDATE_PUBLIC_KEY = ("142a9ebbb7003143f6715b7714a112e74ef989b5899bb59d1309139986744076"
                     "b05553946569a13d1cff7c28942325b7998f04b0942d0dd9ffc7cb374a279494")
UPDATE_APIS = (
    f"https://api.github.com/repos/{APP_REPO_GITHUB}/releases/latest",
    f"https://gitee.com/api/v5/repos/{APP_REPO_GITEE}/releases/latest",
)


def version_key(text):
    """把 v1.2.0 / 1.2 这类版本号拆成可比较的元组"""
    numbers = re.findall(r"\d+", str(text or ""))
    parts = [int(n) for n in numbers[:3]]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def newer_version(candidate, current=None):
    return version_key(candidate) > version_key(current or APP_VERSION)


def build_time_text():
    """构建时间：打包后的 exe 用文件修改时间，源码运行用脚本修改时间"""
    target = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
    try:
        return datetime.fromtimestamp(os.path.getmtime(target)).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return "未知"


def installed_root():
    """安装根目录（…\\倚肆工具 的上一层）；免安装运行时返回 None"""
    folder = app_dir()
    if os.path.basename(folder) != APP_DIR_NAME:
        return None
    return os.path.dirname(folder)


def _http_json(url, timeout=8):
    # User-Agent 只能是 ASCII：中文程序名直接放进去会在 latin-1 编码时炸
    request = urllib.request.Request(
        url, headers={"User-Agent": f"yisi-timer/{APP_VERSION}", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _pick_asset(assets, want_sig):
    """按扩展名挑资产：两个平台的发版都可能改写中文文件名，不能只按名字匹配"""
    for name in sorted(assets):
        lower = name.lower()
        if lower.endswith(".exe.sig") or lower.endswith(".sig"):
            if want_sig:
                return assets[name]
            continue
        if not want_sig and lower.endswith(".exe"):
            return assets[name]
    return None


def parse_release(payload, source=""):
    """把 GitHub / Gitee 的发版 JSON 归一成同一个结构"""
    tag = payload.get("tag_name") or payload.get("name") or ""
    version = str(tag).strip().lstrip("vV")
    assets = {}
    for asset in payload.get("assets") or []:
        name = asset.get("name") or ""
        url = asset.get("browser_download_url") or ""
        if name and url:
            assets[name] = url
    exe_url = assets.get(UPDATE_ASSET) or _pick_asset(assets, False)
    sig_url = assets.get(UPDATE_MANIFEST) or _pick_asset(assets, True)
    if not exe_url:
        raise ValueError("发版里没有安装包")
    if not sig_url:
        raise ValueError("发版里没有签名文件")
    return {
        "version": version,
        "notes": payload.get("body") or "",
        "source": source,
        "exe": exe_url,
        "manifest": sig_url,
    }


def fetch_latest_release(timeout=8):
    """两个源都问一遍取最高版本；某个源失败就用另一个"""
    found, errors = [], []
    for url in UPDATE_APIS:
        try:
            found.append(parse_release(_http_json(url, timeout), url))
        except Exception as exc:  # 网络、限流、发版缺失都算这个源失败
            errors.append(f"{url}：{exc}")
    if not found:
        raise RuntimeError("检查更新失败：" + ("；".join(errors) or "网络不可用"))
    found.sort(key=lambda item: version_key(item["version"]), reverse=True)
    return found[0]


def download_file(url, target, on_progress=None, timeout=20, chunk=64 * 1024):
    request = urllib.request.Request(
        url, headers={"User-Agent": f"yisi-timer/{APP_VERSION}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        with open(target, "wb") as handle:
            while True:
                data = response.read(chunk)
                if not data:
                    break
                handle.write(data)
                done += len(data)
                if on_progress:
                    on_progress(done, total)
    return target


def verify_update(manifest_path, installer_path, expected_version):
    """验签 + 版本 + 大小 + SHA256，任何一项不符都抛异常"""
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    except ImportError as exc:  # 打包漏了验签组件就别装
        raise RuntimeError("程序缺少验签组件，已拒绝安装") from exc

    text = open(manifest_path, encoding="utf-8").read()
    body, marker, signature_line = text.rpartition("signature: ")
    if not marker:
        raise ValueError("更新清单缺少签名")
    if not body.startswith(UPDATE_HEADER):
        raise ValueError("更新清单格式不对")
    raw = bytes.fromhex(signature_line.strip())
    if len(raw) != 64:
        raise ValueError("签名长度不对")

    public = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), b"\x04" + bytes.fromhex(UPDATE_PUBLIC_KEY))
    signature = encode_dss_signature(int.from_bytes(raw[:32], "big"),
                                     int.from_bytes(raw[32:], "big"))
    public.verify(signature, body.encode("utf-8"), ec.ECDSA(hashes.SHA256()))

    fields = {}
    for line in body.splitlines()[1:]:
        if ": " in line:
            key, value = line.split(": ", 1)
            fields[key] = value
    if version_key(fields.get("version")) != version_key(expected_version):
        raise ValueError("清单版本与发版不一致")
    if fields.get("file") != UPDATE_ASSET:
        raise ValueError("清单里的文件名不符")
    data = open(installer_path, "rb").read()
    if int(fields.get("size", "0")) != len(data):
        raise ValueError("安装包大小与清单不符")
    if fields.get("sha256") != hashlib.sha256(data).hexdigest():
        raise ValueError("安装包校验值不符")
    return True


class UpdateCheckTask(QThread):
    """后台问两个源的最新版本，避免卡住界面"""

    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def run(self):
        try:
            self.done.emit(fetch_latest_release())
        except Exception as exc:
            self.failed.emit(str(exc))


class UpdateDownloadTask(QThread):
    """后台下载安装包与签名，下完就地校验"""

    progress = pyqtSignal(int, int)
    done = pyqtSignal(str, str)
    failed = pyqtSignal(str)

    def __init__(self, release, folder, parent=None):
        super().__init__(parent)
        self.release = release
        self.folder = folder

    def run(self):
        manifest_path = os.path.join(self.folder, UPDATE_MANIFEST)
        installer_path = os.path.join(self.folder, UPDATE_ASSET)
        try:
            download_file(self.release["manifest"], manifest_path)
            download_file(self.release["exe"], installer_path, self.progress.emit)
            verify_update(manifest_path, installer_path, self.release["version"])
            self.done.emit(installer_path, manifest_path)
        except Exception as exc:
            self.failed.emit(str(exc))


def launch_update(installer_path):
    """让安装包在程序退出后静默覆盖安装，并把新版本拉起来

    用 PowerShell 而不是批处理：批处理里 `tasklist | find` 在无控制台的分离进程里会直接
    中断（实测批处理只跑到第一行 echo），PowerShell 的 `Wait-Process` 不依赖控制台。
    另外安装包原始文件名带中文，先复制成 `setup.exe` 再执行，避免编码问题。
    """
    root = installed_root()
    if root is None:
        return False, "当前是免安装运行，请到发布页手动下载安装包。"
    work = os.path.join(tempfile.gettempdir(), f"yisi-timer-update-{os.getpid()}")
    try:
        os.makedirs(work, exist_ok=True)
        setup = os.path.join(work, "setup.exe")
        shutil.copyfile(installer_path, setup)
    except OSError as exc:
        return False, f"准备更新包失败：{exc}"

    exe_path = os.path.join(app_dir(), os.path.basename(sys.executable))
    script_path = os.path.join(work, "apply.ps1")
    lines = [
        f"Wait-Process -Id {os.getpid()} -Timeout 300 -ErrorAction SilentlyContinue",
        "Start-Sleep -Milliseconds 400",
        f"Start-Process -FilePath '{setup}' -ArgumentList '--silent', '--dir={root}' -Wait",
    ]
    if getattr(sys, "frozen", False):
        lines.append(f"Start-Process -FilePath '{exe_path}'")
    lines.append("Start-Sleep -Milliseconds 500")
    lines.append(f"Remove-Item -LiteralPath '{work}' -Recurse -Force -ErrorAction SilentlyContinue")
    with open(script_path, "w", encoding="utf-8-sig", newline="\r\n") as handle:
        handle.write("\r\n".join(lines) + "\r\n")

    # CREATE_NO_WINDOW：GUI 程序没有控制台，用 DETACHED_PROCESS 时 PowerShell 会静默不执行
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
             "-File", script_path],
            creationflags=flags, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        return False, f"启动更新程序失败：{exc}"
    return True, ""


def open_in_browser(url):
    QDesktopServices.openUrl(QUrl(url))


# -------------------- 创建托盘图标（文字“卡”） --------------------
def create_icon():
    """托盘与窗口图标：圆角方块 + 强调色描边 + 「卡」字"""
    try:
        t = theme()
        accent, accent2 = QColor(t['accent']), QColor(t['accent2'])
        on_accent = QColor(t['on_accent'])
    except Exception:
        accent, accent2, on_accent = QColor('#cf4813'), QColor('#f97316'), QColor('#ffffff')
    pixmap = QPixmap(128, 128)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    gradient = QLinearGradient(0, 0, 128, 128)
    gradient.setColorAt(0, accent2)
    gradient.setColorAt(1, accent)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(gradient))
    painter.drawRoundedRect(6, 6, 116, 116, 30, 30)
    painter.setBrush(Qt.NoBrush)
    painter.setPen(QPen(QColor(255, 255, 255, 70), 2))
    painter.drawRoundedRect(8, 8, 112, 112, 28, 28)
    painter.setPen(on_accent)
    painter.setFont(QFont("Microsoft YaHei", 54, QFont.DemiBold))
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "卡")
    painter.end()
    return QIcon(pixmap)

# -------------------- 日期显示（公历 + 农历 + 干支 + 生肖） --------------------
def lunar_day_name(day):
    if day <= 10:
        return '初' + '一二三四五六七八九十'[day - 1]
    if day < 20:
        return '十' + '一二三四五六七八九'[day - 11]
    if day == 20:
        return '二十'
    if day < 30:
        return '廿' + '一二三四五六七八九'[day - 21]
    return '三十'

def lunar_parts(now):
    """返回 (农历月日, 干支年, 生肖)；超出范围或缺少依赖时为空串"""
    if ZhDate is None:
        return '', '', ''
    try:
        z = ZhDate.from_datetime(now)
    except Exception:
        return '', '', ''
    month = LUNAR_MONTHS[z.lunar_month - 1]
    if getattr(z, 'leap_month', False):
        month = '闰' + month
    day_text = f"{month}{lunar_day_name(z.lunar_day)}"
    year = int(z.lunar_year)
    ganzhi = f"{GAN[(year - 4) % 10]}{ZHI[(year - 4) % 12]}年"
    zodiac = f"属{ZODIAC[(year - 4) % 12]}"
    if language() == 'en':
        # 英文界面下农历用数字+拼音，中文农历名对英文读者没有意义
        leap = " (leap)" if getattr(z, 'leap_month', False) else ""
        day_text = f"Lunar {z.lunar_month}/{z.lunar_day}{leap}"
        ganzhi = "%s-%s year" % (GAN_PINYIN[(year - 4) % 10], ZHI_PINYIN[(year - 4) % 12])
        zodiac = "Year of the " + ZODIAC_EN[(year - 4) % 12]
    return day_text, ganzhi, zodiac

def _weekday_text(index):
    """0=周一……6=周日，按当前语言返回"""
    if language() == 'en':
        return ("Monday", "Tuesday", "Wednesday", "Thursday",
                "Friday", "Saturday", "Sunday")[index]
    return "星期" + WEEKDAY_CN[index]


def format_datetime(now):
    """按设置拼接卡片下方的时间行，例：2025-11-03 16:04:18  星期一  乙巳年 九月十四 属蛇"""
    cfg = options.get('display') or {}
    date_part = {
        'yy-MM-dd': f"{now:%y-%m-%d}",
        'yyyy年M月d日': f"{now.year}年{now.month}月{now.day}日",
    }.get(cfg.get('date_format'), f"{now:%Y-%m-%d}")
    time_part = f"{now:%H:%M}" if cfg.get('time_format') == 'HH:mm' else f"{now:%H:%M:%S}"
    parts = [f"{date_part} {time_part}"]
    if cfg.get('show_weekday', True):
        parts.append(_weekday_text(now.weekday()))
    if cfg.get('show_lunar', True):
        day_text, ganzhi, zodiac = lunar_parts(now)
        lunar = []
        if ganzhi and cfg.get('show_ganzhi', True):
            lunar.append(ganzhi)
        if day_text:
            lunar.append(day_text)
        if zodiac and cfg.get('show_zodiac', True):
            lunar.append(zodiac)
        if lunar:
            parts.append(' '.join(lunar))
    return '  '.join(parts)

# -------------------- 全屏提醒窗口（深空星尘风格，带重试机制） --------------------
# 配色取自 yisiblog：「落霞红 + 深空蓝 + 月白」
COLOR_LUOXIA = '#cf4813'
COLOR_EMBER = '#f97316'

# -------------------- 主题（卡片与对话框共用一套令牌） --------------------
THEMES = {
    'night': {
        'mode': 'dark',
        'bg1': '#04070f', 'bg2': '#0a1226', 'bg3': '#121e3c',
        'glow_cool': '#6d7cf5', 'glow_warm': '#f97316',
        'star': '#d6e2ff', 'star_alpha': 0.9,
        'dust_halo': '#8c9bff', 'dust_core': '#e4eeff',
        'card_bg_top': 'rgba(255, 255, 255, 8%)', 'card_bg_bottom': 'rgba(255, 255, 255, 4%)',
        'card_border': 'rgba(255, 255, 255, 14%)', 'card_border_top': 'rgba(255, 255, 255, 26%)',
        'title': '#f8f7f0', 'subtitle': 'rgba(203, 213, 245, 92%)',
        'muted': 'rgba(203, 213, 245, 68%)', 'badge': 'rgba(205, 231, 255, 72%)',
        'countdown': '#ffb27a', 'clock': 'rgba(203, 213, 245, 42%)',
        'ghost_text': 'rgba(226, 234, 255, 82%)', 'ghost_border': 'rgba(255, 255, 255, 22%)',
        'ghost_bg': 'rgba(255, 255, 255, 4%)', 'track': 'rgba(255, 255, 255, 14%)',
        'shadow': 170,
        'accent': '#cf4813', 'accent2': '#f97316', 'on_accent': '#fff6ee',
        'accent_soft': 'rgba(249, 115, 22, 12%)', 'accent_border': 'rgba(249, 115, 22, 42%)',
        'accent_border_strong': 'rgba(249, 115, 22, 75%)',
        'dialog_bg': '#0d1424', 'panel_bg': 'rgba(255, 255, 255, 4.5%)',
        'panel_border': 'rgba(255, 255, 255, 8%)',
        'card2_bg': 'rgba(255, 255, 255, 3.5%)', 'card2_border': 'rgba(255, 255, 255, 6%)',
        'input_bg': 'rgba(255, 255, 255, 6%)', 'input_border': 'rgba(255, 255, 255, 12%)',
        'input_text': '#eef2ff',
        'text': '#dbe3f4', 'text_strong': '#f8f7f0',
        'text_muted': 'rgba(203, 213, 245, 74%)', 'text_dim': 'rgba(203, 213, 245, 62%)',
        'success_bg': 'rgba(143, 214, 162, 9%)', 'success_border': 'rgba(143, 214, 162, 26%)',
        'success_text': 'rgba(200, 240, 214, 86%)',
        'btn_bg': 'rgba(255, 255, 255, 8%)', 'btn_border': 'rgba(255, 255, 255, 14%)',
        'hover_bg': 'rgba(255, 255, 255, 7%)', 'scrollbar': 'rgba(255, 255, 255, 16%)',
        'menu_bg': '#0f1729', 'menu_border': 'rgba(255, 255, 255, 12%)',
        'menu_text': '#e6ecff', 'menu_sel': 'rgba(249, 115, 22, 26%)',
    },
    'dawn': {
        'mode': 'light',
        'bg1': '#fbfcff', 'bg2': '#eef2fb', 'bg3': '#e2e9f7',
        'glow_cool': '#b7c4ff', 'glow_warm': '#ffcf9f',
        'star': '#7c8aa8', 'star_alpha': 0.34,
        'dust_halo': '#c7d2fe', 'dust_core': '#93a2d6',
        'card_bg_top': 'rgba(255, 255, 255, 94%)', 'card_bg_bottom': 'rgba(255, 255, 255, 78%)',
        'card_border': 'rgba(23, 33, 54, 10%)', 'card_border_top': 'rgba(255, 255, 255, 96%)',
        'title': '#121826', 'subtitle': 'rgba(38, 48, 72, 86%)',
        'muted': 'rgba(38, 48, 72, 60%)', 'badge': 'rgba(79, 70, 229, 72%)',
        'countdown': '#c2410c', 'clock': 'rgba(38, 48, 72, 46%)',
        'ghost_text': 'rgba(38, 48, 72, 72%)', 'ghost_border': 'rgba(23, 33, 54, 16%)',
        'ghost_bg': 'rgba(255, 255, 255, 55%)', 'track': 'rgba(23, 33, 54, 12%)',
        'shadow': 70,
        'accent': '#cf4813', 'accent2': '#f97316', 'on_accent': '#ffffff',
        'accent_soft': 'rgba(249, 115, 22, 12%)', 'accent_border': 'rgba(249, 115, 22, 40%)',
        'accent_border_strong': 'rgba(249, 115, 22, 70%)',
        'dialog_bg': '#f5f7fc', 'panel_bg': '#ffffff',
        'panel_border': 'rgba(23, 33, 54, 9%)',
        'card2_bg': '#ffffff', 'card2_border': 'rgba(23, 33, 54, 8%)',
        'input_bg': '#ffffff', 'input_border': 'rgba(23, 33, 54, 14%)',
        'input_text': '#1c2333',
        'text': '#2b3446', 'text_strong': '#10151f',
        'text_muted': 'rgba(43, 52, 70, 76%)', 'text_dim': 'rgba(43, 52, 70, 64%)',
        'success_bg': 'rgba(34, 197, 94, 10%)', 'success_border': 'rgba(34, 197, 94, 28%)',
        'success_text': '#15803d',
        'btn_bg': '#ffffff', 'btn_border': 'rgba(23, 33, 54, 14%)',
        'hover_bg': 'rgba(23, 33, 54, 5%)', 'scrollbar': 'rgba(23, 33, 54, 18%)',
        'menu_bg': '#ffffff', 'menu_border': 'rgba(23, 33, 54, 12%)',
        'menu_text': '#1c2333', 'menu_sel': 'rgba(249, 115, 22, 14%)',
    },
    'dusk': {
        'mode': 'dark',
        'bg1': '#170d10', 'bg2': '#2a1214', 'bg3': '#3b1a17',
        'glow_cool': '#a855f7', 'glow_warm': '#ff9a52',
        'star': '#ffe2c2', 'star_alpha': 0.85,
        'dust_halo': '#ffcfa8', 'dust_core': '#fff0dc',
        'card_bg_top': 'rgba(255, 235, 220, 10%)', 'card_bg_bottom': 'rgba(255, 225, 205, 5%)',
        'card_border': 'rgba(255, 210, 180, 16%)', 'card_border_top': 'rgba(255, 220, 190, 30%)',
        'title': '#fff6ec', 'subtitle': 'rgba(255, 228, 210, 90%)',
        'muted': 'rgba(255, 222, 200, 66%)', 'badge': 'rgba(255, 196, 150, 78%)',
        'countdown': '#ffc08a', 'clock': 'rgba(255, 214, 186, 44%)',
        'ghost_text': 'rgba(255, 238, 225, 84%)', 'ghost_border': 'rgba(255, 214, 186, 24%)',
        'ghost_bg': 'rgba(255, 236, 220, 5%)', 'track': 'rgba(255, 224, 196, 16%)',
        'shadow': 190,
        'accent': '#e4551d', 'accent2': '#f97316', 'on_accent': '#fff7f0',
        'accent_soft': 'rgba(249, 115, 22, 14%)', 'accent_border': 'rgba(249, 115, 22, 44%)',
        'accent_border_strong': 'rgba(249, 115, 22, 78%)',
        'dialog_bg': '#170f10', 'panel_bg': 'rgba(255, 235, 220, 5%)',
        'panel_border': 'rgba(255, 222, 196, 10%)',
        'card2_bg': 'rgba(255, 232, 215, 4%)', 'card2_border': 'rgba(255, 222, 196, 7%)',
        'input_bg': 'rgba(255, 235, 220, 7%)', 'input_border': 'rgba(255, 222, 196, 13%)',
        'input_text': '#fff1e6',
        'text': '#f1e0d2', 'text_strong': '#fff8f2',
        'text_muted': 'rgba(241, 220, 202, 76%)', 'text_dim': 'rgba(241, 220, 202, 62%)',
        'success_bg': 'rgba(120, 200, 160, 10%)', 'success_border': 'rgba(120, 200, 160, 26%)',
        'success_text': 'rgba(198, 240, 214, 86%)',
        'btn_bg': 'rgba(255, 235, 220, 8%)', 'btn_border': 'rgba(255, 222, 196, 14%)',
        'hover_bg': 'rgba(255, 235, 220, 7%)', 'scrollbar': 'rgba(255, 230, 208, 18%)',
        'menu_bg': '#221213', 'menu_border': 'rgba(255, 222, 196, 12%)',
        'menu_text': '#f6e6d8', 'menu_sel': 'rgba(249, 115, 22, 28%)',
    },
}

THEME_CHOICES = [('night', '深空'), ('dawn', '晨雾'), ('dusk', '暮色')]


def rgba_of(color, alpha):
    """把 #rrggbb 转成带透明度的 rgba()，用于从强调色派生背景与描边"""
    c = QColor(color)
    return f'rgba({c.red()},{c.green()},{c.blue()},{alpha})'


TRANSLATIONS = {
    "现在：": "Now: ",
    # ---- 首页 ----
    "新建分类": "New category",
    "设置": "Settings",
    "＋ 新建分类": "+ New category",
    "重命名": "Rename",
    "删除分类": "Delete category",
    "＋ 新增": "+ Add",
    "预览": "Preview",
    "编辑": "Edit",
    "删除": "Delete",
    "关闭": "Close",
    "退出程序": "Quit",
    "未分类": "Uncategorized",
    "这个分类下还没有提醒，点「＋ 新增」添加一条。": "No reminders in this category yet. Use \"+ Add\" to create one.",
    "立即开启开机自启": "Enable autostart",
    "正在开启…": "Enabling…",
    "开机自启未开启": "Autostart is off",
    "开机自启已开启": "Autostart is on",
    "已调整顺序": "Order updated",
    "已保存": "Saved",
    "已删除提醒": "Reminder deleted",
    "已删除分类": "Category deleted",
    "已新建分类": "Category created",
    "已重命名": "Renamed",
    "已开启开机自启": "Autostart enabled",
    "自定义提醒": "Custom reminder",
    # ---- 规则对话框 ----
    "新增规则": "New rule",
    "编辑规则": "Edit rule",
    "触发器决定什么时候提醒，动作决定到点做什么。": "Triggers decide when it reminds you; actions decide what happens.",
    "基本信息": "Basics",
    "名称": "Name",
    "分类": "Category",
    "什么时候提醒": "When to remind",
    "触发器": "Trigger",
    "提醒时间": "Reminder time",
    "生效星期": "Weekdays",
    "生效时段": "Active window",
    "只在这段时间内触发，留空表示不限；可跨零点": "Only fires inside this window. Leave empty for any time; crossing midnight is fine.",
    "到点做什么": "What to do",
    "动作": "Action",
    "提示标题": "Title",
    "提示内容": "Content",
    "补充说明": "Extra note",
    "角标": "Badge",
    "按钮文案": "Button text",
    "关机倒计时": "Shutdown countdown",
    "到点关机时先弹提醒，这段时间内不操作就继续关机": "A reminder pops up first; if you do nothing, shutdown continues after this long.",
    "其他": "Other",
    "启用这条规则": "Enable this rule",
    "允许「稍后提醒」": "Allow \"snooze\"",
    "取消": "Cancel",
    "保存": "Save",
    "确定": "OK",
    "知道了": "Got it",
    "删除提醒": "Delete reminder",
    "删除分类": "Delete category",
    "分类重复": "Duplicate category",
    "设置失败": "Settings failed",
    "已启用": "Enabled",
    "已取消": "Cancelled",
    "错误": "Error",
    "卸载": "Uninstall",
    "已卸载": "Uninstalled",
    "卸载未完全完成": "Uninstall incomplete",
    # ---- 触发器 / 动作 / 主题 / 语言 ----
    "定时": "Schedule",
    "开机": "On startup",
    "触发类别": "Trigger type",
    "每日": "Every day",
    "工作日": "Weekdays",
    "自定义": "Custom",
    "开机启动": "On startup",
    "每天": "Every day",
    "关机前拦截": "Before shutdown",
    "弹窗提醒": "Popup reminder",
    "关机": "Shut down",
    "提醒": "Remind",
    "深空": "Midnight",
    "晨雾": "Dawn",
    "暮色": "Dusk",
    "中文": "中文",
    "English": "English",
    "不限": "Any",
    "到": "to",
    "时": "Hour",
    "分": "Minute",
    # ---- 设置页 ----
    "时间显示格式、主题外观、开机自启，以及卸载。": "Display format, appearance, autostart and uninstall.",
    "时间显示格式": "Time format",
    "日期": "Date",
    "时间": "Time",
    "星期": "Weekday",
    "农历": "Lunar",
    "干支年": "Ganzhi",
    "生肖": "Zodiac",
    "外观": "Appearance",
    "主题": "Theme",
    "强调色": "Accent",
    "选择颜色": "Pick color",
    "跟随主题": "Use theme",
    "当前：主题配色": "Current: theme color",
    "主题同时作用于提醒窗口、设置页和托盘菜单；强调色可单独指定，随时可恢复。":
        "Theme applies to the reminder window, settings and tray menu; the accent color can be overridden and reset anytime.",
    "语言": "Language",
    "启动": "Startup",
    "已开启": "On",
    "未开启": "Off",
    "卸载软件": "Uninstall",
    # ---- 托盘 ----
    "打开设置": "Open settings",
    "暂停提醒（1 小时）": "Pause reminders (1 hour)",
    "恢复提醒": "Resume reminders",
    "已暂停": "Paused",
    "暂无定时规则": "No scheduled rules",
    "开机自启": "Start with Windows",
    "✓ 开机自启": "✓ Start with Windows",
    "退出": "Quit",
    # ---- 提醒窗口 ----
    "按 ESC 或点击按钮关闭": "Press ESC or click the button to close",
    "按 ESC 关闭提醒": "Press ESC to close",
    "按 ESC 或点击下方按钮可立即继续关机": "Press ESC or the button below to continue shutdown",
    "按 ESC 或点按钮会立即关机；要中止本次关机请点「取消关机」":
        "ESC or the button continues shutdown; click \"Cancel shutdown\" to abort it",
    "取消关机": "Cancel shutdown",
    "关闭预览": "Close preview",
    "按 ESC 或点击按钮关闭预览，不会关机": "Press ESC or click to close the preview. Nothing will shut down.",
    "预览效果：按钮和 ESC 都只关闭预览，不会关机。": "Preview only: the button and ESC just close this window.",
    "稍后 {0} 分钟": "Snooze {0} min",
    "例如：上班打卡": "e.g. Clock in",
    "08:00，多个用逗号隔开": "08:00, separate multiple times with commas",
    "全屏提醒的大标题，例如：下班打卡": "Big title on the reminder, e.g. Clock out",
    "标题下面的内容，例如：请确认手机企微已打卡": "Text under the title, e.g. Confirm your check-in in WeCom",
    "可选，小字补充说明": "Optional small note",
    "可选，顶部小标签，例如：上班提醒": "Optional badge, e.g. Morning reminder",
    "可选，按钮文案，默认「我知道了」": "Optional button label, defaults to \"Got it\"",
    "例如：会议提醒": "e.g. Meeting reminder",
    "新增提醒": "New reminder",
    "编辑提醒": "Edit reminder",
    "会删除全部提醒配置、开机自启、桌面快捷方式与注册表卸载信息，然后删除程序目录；删除后不可恢复。":
        "This removes all reminder settings, autostart, desktop shortcuts and the uninstall entry, "
        "then deletes the program folder. It cannot be undone.",
    "开机提醒、关机拦截都依赖程序在后台运行；关掉开机自启，重启后这些规则不会触发。":
        "Startup and shutdown rules rely on the app running in the background; "
        "with autostart off they will not fire after a reboot.",
    "开机后自动运行，开机提醒与关机拦截能正常触发。":
        "Starts automatically and startup/shutdown rules work as expected.",
    "开机后自动运行。": "Starts automatically.",
    "重启后程序不会运行，定时提醒也不会触发。":
        "The app will not run after a reboot, so scheduled reminders will not fire.",
    "暂无定时规则": "No scheduled rules",
    "按分类管理提醒：新增、编辑、启用/停用，按住卡片可拖动排序，点「预览」看实际效果。":
        "Manage reminders by category: add, edit, enable or disable. Drag a card to reorder; "
        "click Preview to see the result.",
    "这个分类下还没有提醒，点「＋ 新增」添加一条。":
        "No reminders here yet. Click \"+ Add\" to create one.",
    "开机自启已开启 · {0} 条开机/关机规则会在开机后自动生效":
        "Autostart is on · {0} startup/shutdown rule(s) take effect after a reboot",
    "开机自启未开启 · {0} 条开机/关机规则不会生效：重启后程序不会自动运行，开机提醒与关机拦截都收不到。":
        "Autostart is off · {0} startup/shutdown rule(s) will not work: "
        "the app will not run after a reboot, so neither reminders nor shutdown interception happen.",
    "＋ 新增 ▾": "+ Add ▾",
    "已暂停提醒": "Reminders paused",
    "已恢复提醒": "Reminders resumed",
    # ---- 关于与更新 ----
    "关于": "About",
    "版本": "Version",
    "版本信息": "Version info",
    "构建时间": "Built",
    "位置": "Location",
    "作者": "Author",
    "开源与许可": "Open source & license",
    "打开 GitHub": "Open GitHub",
    "打开 Gitee": "Open Gitee",
    "发布说明": "Release notes",
    "许可证：PolyForm Noncommercial 1.0.0（源码公开，禁止商用）":
        "License: PolyForm Noncommercial 1.0.0 (source-available, no commercial use)",
    "更新": "Update",
    "检查更新时会连接 GitHub 与 Gitee。": "Checking for updates contacts GitHub and Gitee.",
    "检查更新": "Check for updates",
    "下载并安装": "Download and install",
    "正在检查更新…": "Checking for updates…",
    "正在下载安装包…": "Downloading the installer…",
    "签名与校验值核对通过，正在安装…": "Signature and checksum verified, installing…",
    "已是最新版本（{0}）。": "You are on the latest version ({0}).",
    "发现新版本 {0}（当前 {1}），可直接下载安装，会自动关闭程序并重开。":
        "Version {0} is available (current {1}). Installing will restart the app automatically.",
    "检查更新失败，请检查网络。": "Update check failed, please check your network.",
    "下载失败，请稍后重试。": "Download failed, please try again later.",
    "无法自动安装": "Cannot install automatically",
}

import re as _re

_COUNT_RE = _re.compile(r"^(\d+) 条$")
_FOOTER_RE = _re.compile(r"^(\d+) 条提醒 · (\d+) 个分类$")
_TRIGGER_DAILY_RE = _re.compile(r"^每天 (.+)$")
_TRIGGER_WORKDAY_RE = _re.compile(r"^工作日 (.+)$")
_TRIGGER_WEEKLY_RE = _re.compile(r"^每周(.+) (\d{2}:\d{2}.*)$")
_BOOT_DAILY_RE = _re.compile(r"^开机启动（每天）$")
_BOOT_WORKDAY_RE = _re.compile(r"^开机启动（工作日）$")
_BOOT_WEEKLY_RE = _re.compile(r"^开机启动（每周(.+)）$")
_BOOT_WINDOW_RE = _re.compile(r"^(.+) · (.+) 内有效$")
_WINDOW_RE = _re.compile(r"^(.+)（(.+) 内有效）$")
_NEXT_RE = _re.compile(r"^下一条：(.+)$")
_SNOOZE_RE = _re.compile(r"^稍后 (\d+) 分钟$")
_COUNTDOWN_RE = _re.compile(r"^(\d+) 秒后自动继续关机$")
_PREVIEW_COUNTDOWN_RE = _re.compile(r"^(\d+) 秒后自动关闭预览（不会关机）$")
_BANNER_OFF_RE = _re.compile(r"^(\d+) 条开机/关机规则需要程序开机自启才能触发。$")
_SETTINGS_TITLE_RE = _re.compile(r"^(.+) · 设置$")
_AUTOSTART_RE = _re.compile(r"^开机时自动启动 (.+)$")
_UNINSTALL_BTN_RE = _re.compile(r"^卸载 (.+)$")
_WEEKDAY_RE = _re.compile(r"^星期([一二三四五六日])$")

_WEEKDAY_EN = {"一": "Monday", "二": "Tuesday", "三": "Wednesday", "四": "Thursday",
               "五": "Friday", "六": "Saturday", "日": "Sunday"}
_DAY_EN = {"一": "Mon", "二": "Tue", "三": "Wed", "四": "Thu",
           "五": "Fri", "六": "Sat", "日": "Sun"}


def _days_en(text):
    """「一、三」→「Mon, Wed」"""
    return ", ".join(_DAY_EN.get(ch, ch) for ch in text if ch in _DAY_EN)


def language():
    return options.get('language') or 'zh'


def _english():
    return language() == 'en'


def t(text):
    """界面文案翻译：只翻我们自己的固定文案，用户填的规则内容不动"""
    if not text or not _english():
        return text
    if text in TRANSLATIONS:
        return TRANSLATIONS[text]
    match = _COUNT_RE.match(text)
    if match:
        return "%s items" % match.group(1)
    return text


def _translate_dynamic_core(text):
    if not text or not _english():
        return text
    match = _TRIGGER_DAILY_RE.match(text)
    if match:
        return "Every day " + match.group(1)
    match = _TRIGGER_WORKDAY_RE.match(text)
    if match:
        return "Weekdays " + match.group(1)
    match = _TRIGGER_WEEKLY_RE.match(text)
    if match:
        return "%s %s" % (_days_en(match.group(1)), match.group(2))
    match = _BOOT_DAILY_RE.match(text)
    if match:
        return "On startup (every day)"
    match = _BOOT_WORKDAY_RE.match(text)
    if match:
        return "On startup (weekdays)"
    match = _BOOT_WEEKLY_RE.match(text)
    if match:
        return "On startup (%s)" % _days_en(match.group(1))
    match = _BOOT_WINDOW_RE.match(text)
    if match:
        return "%s (only %s)" % (match.group(1), match.group(2))
    match = _WINDOW_RE.match(text)
    if match:
        return "%s (only %s)" % (match.group(1), match.group(2))
    match = _NEXT_RE.match(text)
    if match:
        return "Next: " + match.group(1)
    match = _SNOOZE_RE.match(text)
    if match:
        return "Snooze %s min" % match.group(1)
    match = _COUNTDOWN_RE.match(text)
    if match:
        return "Shutting down in %s s" % match.group(1)
    match = _PREVIEW_COUNTDOWN_RE.match(text)
    if match:
        return "Preview closes in %s s (no shutdown)" % match.group(1)
    match = _BANNER_OFF_RE.match(text)
    if match:
        return "%s startup/shutdown rule(s) need autostart to work." % match.group(1)
    match = _SETTINGS_TITLE_RE.match(text)
    if match:
        return "%s · Settings" % match.group(1)
    match = _AUTOSTART_RE.match(text)
    if match:
        return "Start with Windows"
    match = _UNINSTALL_BTN_RE.match(text)
    if match:
        return "Uninstall %s" % match.group(1)
    match = _WEEKDAY_RE.match(text)
    if match:
        return _WEEKDAY_EN.get(match.group(1), text)
    match = _FOOTER_RE.match(text)
    if match:
        count, cats = match.group(1), match.group(2)
        return "%s %s · %s %s" % (count, "reminder" if count == "1" else "reminders",
                                  cats, "category" if cats == "1" else "categories")
    return t(text)


def _translate_dynamic(text):
    """核心翻译 + 卡片副标题里的动作词"""
    result = _translate_dynamic_core(text)
    if _english():
        result = result.replace(" · 提醒", " · remind").replace(" · 关机", " · shut down")
    return result


def _user_texts():
    """用户自己填的内容，翻译时要跳过"""
    found = set()
    for rule in rules:
        found.add(rule.get('name') or '')
        action = rule.get('action') or {}
        for key in ('title', 'content', 'hint', 'badge', 'button_text'):
            found.add(action.get(key) or '')
        for value in (rule.get('trigger') or {}).get('times') or []:
            found.add(str(value))
    for cat in categories:
        found.add(cat.get('name') or '')
    for value in (options.get('display') or {}).values():
        if isinstance(value, str):
            found.add(value)
    found.discard('')
    return found


def translate_widgets(root):
    """把窗口里所有固定文案换成当前语言（用户数据不动）"""
    if not _english():
        return
    skip = _user_texts()

    def fix(widget):
        try:
            text = widget.text()
        except Exception:
            return
        if not text or text in skip:
            return
        if isinstance(widget, (QLabel, QPushButton, QToolButton, QCheckBox, QRadioButton)):
            new = _translate_dynamic(text)
            if new != text:
                widget.setText(new)
        elif widget.metaObject().className() == "QGroupBox":
            new = _translate_dynamic(text)
            if new != text:
                widget.setTitle(new)

    for widget in root.findChildren(QWidget):
        fix(widget)
        if isinstance(widget, (QLineEdit, QPlainTextEdit)):
            placeholder = widget.placeholderText()
            if placeholder and placeholder not in skip:
                new_placeholder = t(placeholder)
                if new_placeholder != placeholder:
                    widget.setPlaceholderText(new_placeholder)
        if isinstance(widget, QComboBox):
            for index in range(widget.count()):
                text = widget.itemText(index)
                if text in skip:
                    continue
                new = _translate_dynamic(text)
                if new != text:
                    widget.setItemText(index, new)
    if isinstance(root, QWidget) and root.windowTitle():
        title = root.windowTitle()
        if title not in skip:
            root.setWindowTitle(_translate_dynamic(title))


def translate_menu(menu_obj):
    if menu_obj is None or not _english():
        return
    for action in menu_obj.actions():
        text = action.text()
        if text:
            action.setText(_translate_dynamic(text))
        submenu = action.menu()
        if submenu is not None:
            translate_menu(submenu)


def apply_language_everywhere():
    """切换语言后重建已经打开的界面（固定文案是代码里的中文，重建再翻译最稳）"""
    home = globals().get('_settings_window')
    if home is not None and hasattr(home, 'reload'):
        home.reload()
    menu_obj = globals().get('menu')
    if menu_obj is not None:
        update_menu()
    tray_obj = globals().get('tray')
    if tray_obj is not None:
        tray_obj.setToolTip(APP_NAME)


def theme():
    """当前主题令牌：预设 + 可选自定义强调色（强调色会派生按钮、进度条、选中色）"""
    chosen = options.get('theme') or {}
    name = chosen.get('name') if chosen.get('name') in THEMES else 'night'
    tokens = dict(THEMES[name])
    tokens['name'] = name
    accent = str(chosen.get('accent') or '').strip()
    if accent and QColor(accent).isValid():
        tokens['accent'] = accent
        tokens['accent2'] = QColor(accent).lighter(118).name()
        tokens['accent_hover'] = QColor(accent).lighter(112).name()
        tokens['accent2_hover'] = QColor(accent).lighter(126).name()
        tokens['accent_pressed'] = QColor(accent).darker(112).name()
        tokens['accent2_pressed'] = QColor(accent).darker(104).name()
        tokens['accent_soft'] = rgba_of(accent, 0.12)
        tokens['accent_border'] = rgba_of(accent, 0.42)
        tokens['accent_border_strong'] = rgba_of(accent, 0.75)
        tokens['menu_sel'] = rgba_of(accent, 0.26 if tokens['mode'] == 'dark' else 0.14)
    else:
        tokens['accent_hover'] = QColor(tokens['accent2']).lighter(110).name()
        tokens['accent2_hover'] = QColor(tokens['accent2']).lighter(112).name()
        tokens['accent_pressed'] = QColor(tokens['accent']).darker(115).name()
        tokens['accent2_pressed'] = QColor(tokens['accent2']).darker(108).name()
    tokens['accent_fill'] = (rgba_of(tokens['accent'], 0.12) if tokens['mode'] == 'light'
                             else rgba_of(tokens['accent2'], 0.18))
    if tokens['mode'] == 'light':
        tokens['warn_text'] = '#a3430a'
        tokens['danger_bg'] = 'rgba(220, 38, 38, 10%)'
        tokens['danger_border'] = 'rgba(220, 38, 38, 32%)'
        tokens['danger_hover'] = 'rgba(220, 38, 38, 20%)'
        tokens['danger_text'] = '#b91c1c'
    else:
        tokens['warn_text'] = '#ffd7b8'
        tokens['danger_bg'] = 'rgba(220, 38, 38, 16%)'
        tokens['danger_border'] = 'rgba(220, 38, 38, 42%)'
        tokens['danger_hover'] = 'rgba(220, 38, 38, 30%)'
        tokens['danger_text'] = '#ffc9c9'
    return tokens


_rand = random.Random(20261103)

# (x 比例, y 比例, 半径, 相位, 闪烁速度)
STAR_FIELD = [(
    _rand.uniform(0.02, 0.98),
    _rand.uniform(0.03, 0.97),
    _rand.uniform(0.6, 1.9),
    _rand.uniform(0, math.tau),
    _rand.uniform(0.6, 1.8),
) for _ in range(78)]

# (x 比例, 初始 y 比例, 上升速度, 半径, 相位, 亮度)
DUST_FIELD = [(
    _rand.uniform(0.02, 0.98),
    _rand.uniform(0.0, 1.0),
    _rand.uniform(0.008, 0.022),
    _rand.uniform(1.1, 3.2),
    _rand.uniform(0, math.tau),
    _rand.uniform(0.25, 0.9),
) for _ in range(36)]


def _glow_pixmap(size, color):
    """预渲染柔光贴图：运行时只做缩放叠加，避免每帧重算径向渐变"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    g = QRadialGradient(size / 2, size / 2, size / 2)
    core = QColor(color)
    core.setAlpha(120)
    mid = QColor(color)
    mid.setAlpha(52)
    edge = QColor(color)
    edge.setAlpha(0)
    g.setColorAt(0.0, core)
    g.setColorAt(0.45, mid)
    g.setColorAt(1.0, edge)
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(g))
    p.drawEllipse(0, 0, size, size)
    p.end()
    return pm


class ReminderWindow(QWidget):
    closed_manually = pyqtSignal()  # 用户主动关闭时发射
    confirmed = pyqtSignal()        # 点了确认按钮或按 ESC 立即继续时发射
    snoozed = pyqtSignal()          # 点了“稍后提醒”时发射
    extra_clicked = pyqtSignal()    # 点了额外按钮（如“取消关机”）时发射

    def __init__(self, title='提醒', content='', hint='', button_text=None,
                 countdown=None, badge='提醒', snooze_minutes=None, countdown_text=None,
                 esc_hint=None, extra_text=None):
        super().__init__()
        self._title = (str(title) or '提醒').strip()
        self._extras = [ln.strip() for ln in str(content or '').split('\n') if ln.strip()]
        self._snooze_minutes = int(snooze_minutes) if snooze_minutes else None
        self._countdown = int(countdown) if countdown else None
        self._countdown_text = countdown_text or "{n} 秒后自动继续关机"
        self._esc_hint = esc_hint
        self._extra_text = extra_text
        self._remaining = self._countdown
        self._bg = None
        self._glow_cool = None
        self._glow_warm = None
        self._finished = False
        self._t0 = time.monotonic()
        self._t = theme()

        self.setWindowTitle("提醒")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAutoFillBackground(False)
        self.showFullScreen()

        screen = QApplication.primaryScreen().geometry()
        sw, sh = screen.width(), screen.height()
        card_width = min(1020, int(sw * 0.62))
        title_px = max(46, min(104, int(sh * 0.082)))
        extra_px = max(18, min(32, int(sh * 0.026)))
        badge_px = max(13, min(18, int(sh * 0.0145)))
        count_px = max(24, min(48, int(sh * 0.040)))
        note_px = max(14, min(21, int(sh * 0.017)))
        button_px = max(18, min(26, int(sh * 0.022)))

        t = self._t = theme()
        self.setStyleSheet(f"""
            QFrame#card {{
                background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {t['card_bg_top']}, stop:1 {t['card_bg_bottom']});
                border: 1px solid {t['card_border']};
                border-top: 1px solid {t['card_border_top']};
                border-radius: 30px;
            }}
            QLabel#badge {{ color: {t['badge']}; }}
            QLabel#title {{ color: {t['title']}; }}
            QLabel#subtitle {{ color: {t['subtitle']}; }}
            QLabel#countdown {{ color: {t['countdown']}; }}
            QLabel#footnote {{ color: {t['muted']}; }}
            QLabel#clock {{ color: {t['clock']}; }}
            QPushButton#primary {{
                color: {t['on_accent']};
                border: none;
                border-radius: 24px;
                padding: 14px 52px;
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {t['accent2']}, stop:1 {t['accent']});
            }}
            QPushButton#primary:hover {{
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {t['accent2_hover']}, stop:1 {t['accent_hover']});
            }}
            QPushButton#primary:pressed {{
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {t['accent2_pressed']}, stop:1 {t['accent_pressed']});
            }}
            QPushButton#ghost {{
                color: {t['ghost_text']};
                border: 1px solid {t['ghost_border']};
                border-radius: 24px;
                padding: 14px 38px;
                background-color: {t['ghost_bg']};
            }}
            QPushButton#ghost:hover {{
                color: {t['title']};
                border-color: {t['card_border_top']};
                background-color: {t['hover_bg']};
            }}
            QPushButton#ghost:pressed {{
                background-color: {t['track']};
            }}
            QProgressBar {{
                background-color: {t['track']};
                border: none;
                border-radius: 4px;
            }}
            QProgressBar::chunk {{
                border-radius: 4px;
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {t['accent2']}, stop:1 {t['accent']});
            }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(40, 36, 40, 28)
        root.setSpacing(0)
        root.addStretch(1)

        card = QFrame(self)
        card.setObjectName('card')
        card.setFixedWidth(card_width)
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(90)
        shadow.setOffset(0, 26)
        shadow.setColor(QColor(0, 0, 0, t['shadow']))
        card.setGraphicsEffect(shadow)

        pad_x = int(card_width * 0.09)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(pad_x, int(card_width * 0.062), pad_x, int(card_width * 0.058))
        inner.setSpacing(max(14, int(sh * 0.016)))

        badge_label = QLabel(badge, card)
        badge_label.setObjectName('badge')
        badge_font = QFont('Microsoft YaHei UI')
        badge_font.setPixelSize(badge_px)
        badge_font.setWeight(QFont.DemiBold)
        badge_font.setLetterSpacing(QFont.AbsoluteSpacing, 4)
        badge_label.setFont(badge_font)
        badge_label.setAlignment(Qt.AlignHCenter)
        inner.addWidget(badge_label)

        title_label = QLabel(self._title, card)
        title_label.setObjectName('title')
        title_label.setAlignment(Qt.AlignHCenter)
        title_label.setWordWrap(True)
        title_label.setFont(self._fit_font(self._title, title_px, card_width - 2 * pad_x))
        inner.addWidget(title_label)
        inner.addSpacing(int(sh * 0.004))

        for extra in self._extras:
            extra_label = QLabel(extra, card)
            extra_label.setObjectName('subtitle')
            extra_label.setAlignment(Qt.AlignHCenter)
            extra_label.setWordWrap(True)
            extra_font = QFont('Microsoft YaHei UI')
            extra_font.setPixelSize(extra_px)
            extra_label.setFont(extra_font)
            inner.addWidget(extra_label)

        if hint:
            hint_label = QLabel(hint, card)
            hint_label.setObjectName('footnote')
            hint_label.setAlignment(Qt.AlignHCenter)
            hint_label.setWordWrap(True)
            hint_font = QFont('Microsoft YaHei UI')
            hint_font.setPixelSize(note_px)
            hint_label.setFont(hint_font)
            inner.addWidget(hint_label)

        self.countdown_label = None
        self._progress = None
        if self._countdown:
            self.countdown_label = QLabel('', card)
            self.countdown_label.setObjectName('countdown')
            self.countdown_label.setAlignment(Qt.AlignHCenter)
            count_font = QFont('Microsoft YaHei UI')
            count_font.setPixelSize(count_px)
            count_font.setWeight(QFont.Bold)
            self.countdown_label.setFont(count_font)
            inner.addSpacing(int(sh * 0.008))
            inner.addWidget(self.countdown_label)

            self._progress = QProgressBar(card)
            self._progress.setRange(0, self._countdown)
            self._progress.setValue(self._countdown)
            self._progress.setTextVisible(False)
            self._progress.setFixedHeight(8)
            self._progress.setFixedWidth(int(card_width * 0.58))
            progress_row = QHBoxLayout()
            progress_row.addStretch(1)
            progress_row.addWidget(self._progress)
            progress_row.addStretch(1)
            inner.addLayout(progress_row)

            esc_label = QLabel(self._esc_hint or "按 ESC 或点击下方按钮可立即继续关机", card)
            esc_label.setObjectName('footnote')
            esc_label.setAlignment(Qt.AlignHCenter)
            esc_font = QFont('Microsoft YaHei UI')
            esc_font.setPixelSize(note_px)
            esc_label.setFont(esc_font)
            inner.addWidget(esc_label)
        else:
            esc_label = QLabel("按 ESC 或点击按钮关闭" if button_text else "按 ESC 关闭提醒", card)
            esc_label.setObjectName('footnote')
            esc_label.setAlignment(Qt.AlignHCenter)
            esc_font = QFont('Microsoft YaHei UI')
            esc_font.setPixelSize(note_px)
            esc_label.setFont(esc_font)
            inner.addWidget(esc_label)

        primary_text = button_text or ("我知道了" if self._snooze_minutes else None)
        if primary_text or self._snooze_minutes or self._extra_text:
            button_row = QHBoxLayout()
            button_row.setSpacing(int(sw * 0.010))
            button_row.addStretch(1)
            if primary_text:
                self.button = QPushButton(primary_text, card)
                self.button.setObjectName('primary')
                self.button.setCursor(Qt.PointingHandCursor)
                button_font = QFont('Microsoft YaHei UI')
                button_font.setPixelSize(button_px)
                button_font.setWeight(QFont.DemiBold)
                self.button.setFont(button_font)
                self.button.setMinimumHeight(int(sh * 0.062))
                self.button.clicked.connect(self._confirm)
                button_row.addWidget(self.button)
            if self._snooze_minutes:
                self.snooze_button = QPushButton(f"稍后 {self._snooze_minutes} 分钟", card)
                self.snooze_button.setObjectName('ghost')
                self.snooze_button.setCursor(Qt.PointingHandCursor)
                snooze_font = QFont('Microsoft YaHei UI')
                snooze_font.setPixelSize(button_px)
                snooze_font.setWeight(QFont.DemiBold)
                self.snooze_button.setFont(snooze_font)
                self.snooze_button.setMinimumHeight(int(sh * 0.062))
                self.snooze_button.clicked.connect(self._snooze)
                button_row.addWidget(self.snooze_button)
            if self._extra_text:
                self.extra_button = QPushButton(self._extra_text, card)
                self.extra_button.setObjectName('ghost')
                self.extra_button.setCursor(Qt.PointingHandCursor)
                extra_font = QFont('Microsoft YaHei UI')
                extra_font.setPixelSize(button_px)
                extra_font.setWeight(QFont.DemiBold)
                self.extra_button.setFont(extra_font)
                self.extra_button.setMinimumHeight(int(sh * 0.062))
                self.extra_button.clicked.connect(self._extra)
                button_row.addWidget(self.extra_button)
            button_row.addStretch(1)
            inner.addSpacing(int(sh * 0.006))
            inner.addLayout(button_row)

        root.addWidget(card, 0, Qt.AlignHCenter)

        self._clock_label = QLabel('', self)
        self._clock_label.setObjectName('clock')
        self._clock_label.setAlignment(Qt.AlignHCenter)
        clock_font = QFont('Microsoft YaHei UI')
        clock_font.setPixelSize(note_px)
        clock_font.setLetterSpacing(QFont.AbsoluteSpacing, 1)
        self._clock_label.setFont(clock_font)
        root.addSpacing(int(sh * 0.024))
        root.addWidget(self._clock_label)
        root.addStretch(1)

        self._update_clock()
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._update_clock)
        self._clock_timer.start(1000)

        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self.update)
        self._anim_timer.start(40)

        if self._countdown:
            self._tick_timer = QTimer(self)
            self._tick_timer.timeout.connect(self._tick)
            self._tick_timer.start(1000)
            self._update_countdown()

        self.activateWindow()
        self.raise_()
        self.setFocus()

    # ---------- 背景特效 ----------
    def showEvent(self, event):
        super().showEvent(event)
        self._t0 = time.monotonic()
        apply_translation = globals().get('translate_widgets')
        if apply_translation is not None:
            apply_translation(self)
        # 有的机器上全屏窗口不会自动成为活动窗口，补一次，保证按钮和 ESC 能收到输入
        self.raise_()
        self.activateWindow()
        if getattr(self, '_anim_timer', None):
            self._anim_timer.start(40)

    def hideEvent(self, event):
        if getattr(self, '_anim_timer', None):
            self._anim_timer.stop()
        super().hideEvent(event)

    def resizeEvent(self, event):
        self._bg = None
        super().resizeEvent(event)

    def _ensure_background(self):
        if self._bg is not None and self._bg.size() == self.size():
            return
        w, h = max(1, self.width()), max(1, self.height())
        pm = QPixmap(w, h)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)

        base = QLinearGradient(0, 0, w * 0.35, h)
        base.setColorAt(0.0, QColor(self._t['bg1']))
        base.setColorAt(0.45, QColor(self._t['bg2']))
        base.setColorAt(1.0, QColor(self._t['bg3']))
        p.fillRect(0, 0, w, h, QBrush(base))

        cold_color = QColor(self._t['glow_cool'])
        cold_color.setAlpha(70)
        cold_fade = QColor(cold_color)
        cold_fade.setAlpha(22)
        cold = QRadialGradient(w * 0.78, h * 0.10, max(w, h) * 0.62)
        cold.setColorAt(0.0, cold_color)
        cold.setColorAt(0.55, cold_fade)
        cold.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(0, 0, w, h, QBrush(cold))

        warm_color = QColor(self._t['accent'])
        warm_color.setAlpha(62)
        warm_fade = QColor(warm_color)
        warm_fade.setAlpha(20)
        warm = QRadialGradient(w * 0.28, h * 1.04, max(w, h) * 0.72)
        warm.setColorAt(0.0, warm_color)
        warm.setColorAt(0.50, warm_fade)
        warm.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(0, 0, w, h, QBrush(warm))

        vignette = QRadialGradient(w / 2, h / 2, max(w, h) * 0.78)
        vignette.setColorAt(0.0, QColor(0, 0, 0, 0))
        vignette.setColorAt(0.62, QColor(0, 0, 0, 22 if self._t['mode'] == 'light' else 44))
        vignette.setColorAt(1.0, QColor(0, 0, 0, 70 if self._t['mode'] == 'light' else 184))
        p.fillRect(0, 0, w, h, QBrush(vignette))
        p.end()
        self._bg = pm

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        self._ensure_background()
        painter.drawPixmap(0, 0, self._bg)

        if self._glow_cool is None:
            self._glow_cool = _glow_pixmap(512, QColor(self._t['glow_cool']))
            self._glow_warm = _glow_pixmap(512, QColor(self._t['glow_warm']))

        w, h = self.width(), self.height()
        t = time.monotonic() - self._t0

        # 两团缓慢呼吸、漂移的星云
        size = int(w * 0.88)
        painter.setOpacity(0.42 + 0.10 * math.sin(t * 0.45))
        painter.drawPixmap(
            int(w * 0.46 - size / 2 + math.sin(t * 0.11) * w * 0.03),
            int(-size * 0.30 + math.cos(t * 0.09) * h * 0.02),
            size, size, self._glow_cool)
        painter.setOpacity(0.38 + 0.10 * math.sin(t * 0.33 + 1.7))
        painter.drawPixmap(
            int(w * 0.16 - size / 2 + math.cos(t * 0.13) * w * 0.03),
            int(h * 0.86 - size * 0.55),
            size, size, self._glow_warm)
        painter.setOpacity(1.0)

        # 闪烁星点
        painter.setPen(Qt.NoPen)
        for xr, yr, radius, phase, speed in STAR_FIELD:
            twinkle = 0.5 + 0.5 * math.sin(t * speed + phase)
            color = QColor(self._t['star'])
            color.setAlpha(int(self._t['star_alpha'] * (60 + 165 * twinkle)))
            painter.setBrush(color)
            painter.drawEllipse(QPointF(xr * w, yr * h), radius, radius)

        # 缓缓上升的尘埃
        for xr, y0, vy, radius, phase, bright in DUST_FIELD:
            yy = (y0 - t * vy) % 1.0
            xx = xr + math.sin(t * 0.5 + phase) * 0.012
            cx, cy = xx * w, yy * h
            halo = QColor(self._t['dust_halo'])
            halo.setAlpha(int(42 * bright))
            painter.setBrush(halo)
            painter.drawEllipse(QPointF(cx, cy), radius * 2.6, radius * 2.6)
            core = QColor(self._t['dust_core'])
            core.setAlpha(int(150 * bright))
            painter.setBrush(core)
            painter.drawEllipse(QPointF(cx, cy), radius, radius)
        painter.end()

    # ---------- 文本与倒计时 ----------
    def _fit_font(self, text, base_px, max_width):
        """按最长一行自动缩小字号，避免长文案超出卡片"""
        size = base_px
        while size > 30:
            font = QFont('Microsoft YaHei UI')
            font.setPixelSize(size)
            font.setWeight(QFont.DemiBold)
            font.setLetterSpacing(QFont.AbsoluteSpacing, 4)
            if QFontMetrics(font).horizontalAdvance(text) <= max_width:
                return font
            size -= 4
        font = QFont('Microsoft YaHei UI')
        font.setPixelSize(30)
        font.setWeight(QFont.DemiBold)
        return font

    def _update_clock(self):
        self._clock_label.setText(format_datetime(datetime.now()))

    def _update_countdown(self):
        if self.countdown_label is None:
            return
        self.countdown_label.setText(self._countdown_text.format(n=self._remaining))
        if self._progress is not None:
            self._progress.setValue(self._remaining)

    def _tick(self):
        self._remaining -= 1
        if self._remaining <= 0:
            self._confirm()
            return
        self._update_countdown()

    def _stop_timers(self):
        for name in ('_tick_timer', '_anim_timer', '_clock_timer'):
            timer = getattr(self, name, None)
            if timer:
                timer.stop()

    def _confirm(self):
        if getattr(self, '_finished', False):
            return
        self._finished = True
        self._stop_timers()
        self.confirmed.emit()
        self.close()

    def _snooze(self):
        if getattr(self, '_finished', False):
            return
        self._finished = True
        self._stop_timers()
        self.snoozed.emit()
        self.close()

    def _extra(self):
        """额外按钮（“取消关机”）：只发信号并关窗，由调用方决定后续动作"""
        if getattr(self, '_finished', False):
            return
        self._finished = True
        self._stop_timers()
        self.extra_clicked.emit()
        self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            if self.countdown_label is not None:
                # 关机拦截窗口：ESC 等同于“立即继续关机”
                self._confirm()
            else:
                self.closed_manually.emit()  # 发射信号，停止重试定时器
                self.close()
        else:
            super().keyPressEvent(event)

# -------------------- 提醒窗口与重试 --------------------
def stop_retry():
    """停止重试定时器"""
    global retry_timer
    if retry_timer:
        retry_timer.stop()
        retry_timer = None

def start_retry(rule):
    """启动重试定时器：5 秒后若还没人处理，重新弹一次，避免错过提醒"""
    global retry_timer
    stop_retry()
    retry_timer = QTimer()
    retry_timer.setSingleShot(True)
    retry_timer.timeout.connect(lambda: retry_show(rule))
    retry_timer.start(5000)

def retry_show(rule):
    """重试显示：关闭旧窗口并重新创建"""
    global _reminder_window
    if _reminder_window:
        try:
            _reminder_window.closed_manually.disconnect()
        except Exception:
            pass
        _reminder_window.close()
        _reminder_window = None
    show_reminder(rule)

def build_reminder_window(rule, hint_override=None):
    """按规则里的提醒动作构造窗口"""
    action = rule.get('action') or {}
    hint = action.get('hint', '') if hint_override is None else hint_override
    return ReminderWindow(
        action.get('title') or rule.get('name') or '提醒',
        action.get('content', ''),
        hint=hint,
        button_text=action.get('button_text') or None,
        badge=action.get('badge') or '提醒',
        snooze_minutes=action.get('snooze_minutes') or None,
    )

def show_reminder(rule):
    """按规则弹出全屏提醒，并启动 5 秒重试"""
    global _reminder_window
    if paused_until and datetime.now() < paused_until:
        _log(f"提醒被暂停，跳过：{rule.get('name', '')}")
        return
    if _reminder_window:
        _reminder_window.close()
        _reminder_window = None
    try:
        win = build_reminder_window(rule)
        win.closed_manually.connect(cleanup_reminder)
        win.confirmed.connect(cleanup_reminder)
        win.snoozed.connect(lambda r=rule: snooze_reminder(r))
        _reminder_window = win
        start_retry(rule)
    except Exception as e:
        print("创建提醒窗口失败：", e)
        start_retry(rule)

def snooze_reminder(rule):
    """稍后提醒：停掉重试，N 分钟后再弹一次"""
    global snooze_timer, _reminder_window
    stop_retry()
    _reminder_window = None
    action = rule.get('action') or {}
    minutes = int(action.get('snooze_minutes') or DEFAULT_SNOOZE_MINUTES)
    minutes = max(1, minutes)
    if snooze_timer:
        snooze_timer.stop()
    snooze_timer = QTimer()
    snooze_timer.setSingleShot(True)
    snooze_timer.timeout.connect(lambda: show_reminder(rule))
    snooze_timer.start(minutes * 60 * 1000)

def cleanup_reminder():
    """用户按 ESC 或点“我知道了”后，清理资源并停止重试"""
    global _reminder_window
    stop_retry()
    _reminder_window = None

# -------------------- 预览 --------------------
# 托盘菜单点「预览」只看效果：不写日志、不触发重试、不真的关机
_preview_window = None

def preview_rule(rule):
    """预览某个规则实际弹出的窗口"""
    global _preview_window
    if _preview_window is not None:
        _preview_window.close()
        _preview_window = None
    action = rule.get('action') or {}
    if action.get('type') == ACTION_SHUTDOWN:
        preview_shutdown(rule)
        return
    _preview_window = build_reminder_window(rule)
    _preview_window.closed_manually.connect(_clear_preview_window)
    _preview_window.confirmed.connect(_clear_preview_window)
    _preview_window.snoozed.connect(_clear_preview_window)

def _clear_preview_window():
    global _preview_window
    _preview_window = None

# -------------------- 开机提醒（可有多条开机规则） --------------------
_boot_window = None
_boot_queue = []

def show_boot_reminders(rule=None):
    """程序启动（含开机自启）时依次弹出开机类规则"""
    global _boot_queue
    if rule is not None:
        _show_boot_rule(rule)
        return
    if _boot_window is not None:
        return
    today = datetime.now().weekday()
    _boot_queue = [r for r in rules
                   if r.get('enabled', True)
                   and trigger_kind(r.get('trigger')) == TRIGGER_BOOT
                   and today in rule_weekdays(r)
                   and within_window(r)]
    _show_next_boot()

def _show_next_boot():
    global _boot_queue
    if _boot_window is not None or not _boot_queue:
        return
    _show_boot_rule(_boot_queue.pop(0))

def _show_boot_rule(rule):
    global _boot_window
    if _boot_window is not None:
        return
    _boot_window = build_reminder_window(rule)
    _boot_window.closed_manually.connect(_clear_boot_window)
    _boot_window.confirmed.connect(_clear_boot_window)
    _boot_window.snoozed.connect(_clear_boot_window)

def _clear_boot_window():
    global _boot_window
    _boot_window = None
    QTimer.singleShot(150, _after_boot_closed)

def _after_boot_closed():
    """开机提醒都关掉后，把首页带到前面来（启动时已经打开过一次）"""
    if _boot_queue:
        _show_next_boot()
        return
    open_settings()

# -------------------- 定时关机 --------------------
# 2026-11-04 起不再拦截系统发起的关机：真实关机流程里 WM_QUERYENDSESSION 不会送到
# 隐藏窗口，SM_SHUTTINGDOWN 兜底也没赶上，实测两次都直接关机——拦不住却让人以为会被拦，
# 比没有更糟。现在只保留「定时关机」：到点先弹提醒，倒计时结束才真正关机，期间可取消。
EWX_POWEROFF = 0x00000008
EWX_FORCEIFHUNG = 0x00000010

user32 = ctypes.WinDLL('user32', use_last_error=True)
advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

user32.ExitWindowsEx.argtypes = [ctypes.wintypes.UINT, ctypes.wintypes.DWORD]
user32.ExitWindowsEx.restype = ctypes.wintypes.BOOL
advapi32.AbortSystemShutdownW.argtypes = [ctypes.wintypes.LPCWSTR]
advapi32.AbortSystemShutdownW.restype = ctypes.wintypes.BOOL

DRY_RUN = ('--dry-run' in sys.argv) or os.environ.get('WARNINGWORK_DRY_RUN') == '1'
# 正在显示的定时关机提醒窗口
_shutdown_window = None
# 触发这次关机的规则，用于日志和标题
_shutdown_rule = None


def _log_paths():
    """日志同时写程序目录和本地数据目录：程序目录不可写时还有一份可查"""
    paths = [os.path.join(os.path.dirname(CONFIG_FILE), 'shutdown_guard.log')]
    local = os.environ.get('LOCALAPPDATA')
    if local:
        paths.append(os.path.join(local, APP_NAME, 'shutdown_guard.log'))
    return paths


def _log(message):
    """定时关机/自启等后台动作写日志，方便事后核对"""
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n"
    wrote = False
    for path in _log_paths():
        try:
            folder = os.path.dirname(path)
            if folder and not os.path.isdir(folder):
                os.makedirs(folder, exist_ok=True)
            with open(path, 'a', encoding='utf-8') as f:
                f.write(line)
            wrote = True
        except Exception:
            continue
    if not wrote:
        try:
            sys.stderr.write(line)
        except Exception:
            pass


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.wintypes.DWORD), ("HighPart", ctypes.wintypes.LONG)]


class _LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", _LUID), ("Attributes", ctypes.wintypes.DWORD)]


class _TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", ctypes.wintypes.DWORD),
                ("Privileges", _LUID_AND_ATTRIBUTES * 1)]


kernel32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
advapi32.OpenProcessToken.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD,
                                      ctypes.POINTER(ctypes.wintypes.HANDLE)]
advapi32.OpenProcessToken.restype = ctypes.wintypes.BOOL
advapi32.LookupPrivilegeValueW.argtypes = [ctypes.wintypes.LPCWSTR,
                                           ctypes.wintypes.LPCWSTR,
                                           ctypes.POINTER(_LUID)]
advapi32.LookupPrivilegeValueW.restype = ctypes.wintypes.BOOL
advapi32.AdjustTokenPrivileges.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.BOOL,
                                           ctypes.POINTER(_TOKEN_PRIVILEGES),
                                           ctypes.wintypes.DWORD, ctypes.c_void_p,
                                           ctypes.c_void_p]
advapi32.AdjustTokenPrivileges.restype = ctypes.wintypes.BOOL
kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]


def _enable_shutdown_privilege():
    """ExitWindowsEx 需要 SeShutdownPrivilege，普通用户默认没有启用"""
    TOKEN_QUERY = 0x0008
    TOKEN_ADJUST_PRIVILEGES = 0x0020
    SE_PRIVILEGE_ENABLED = 0x00000002
    token = ctypes.wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                     TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                     ctypes.byref(token)):
        return False
    try:
        luid = _LUID()
        if not advapi32.LookupPrivilegeValueW(None, "SeShutdownPrivilege", ctypes.byref(luid)):
            return False
        tp = _TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        return bool(advapi32.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None))
    finally:
        kernel32.CloseHandle(token)


def shutdown_rule():
    """第一条启用的「定时关机」规则，供预览与默认值使用"""
    for rule in rules:
        if rule.get('enabled', True) and (rule.get('action') or {}).get('type') == ACTION_SHUTDOWN:
            return rule
    return None


def shutdown_seconds(rule):
    action = (rule or {}).get('action') or {}
    try:
        return max(5, int(action.get('auto_release_seconds') or DEFAULT_AUTO_RELEASE_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_AUTO_RELEASE_SECONDS


def trigger_shutdown(rule=None):
    """定时关机到点：先弹提醒，倒计时结束才关机，期间可以取消"""
    global _shutdown_window, _shutdown_rule
    if _shutdown_window is not None:
        return
    _shutdown_rule = rule
    action = (rule or {}).get('action') or {}
    seconds = shutdown_seconds(rule)
    title = action.get('title') or ((rule or {}).get('name') or '定时关机')
    _log(f"定时关机触发：{title}（{seconds} 秒后关机）")
    _shutdown_window = ReminderWindow(
        title,
        action.get('content', ''),
        hint=action.get('hint', '') or f"{seconds} 秒后自动关机，点「取消关机」可以中止",
        button_text=action.get('button_text') or '立即关机',
        countdown=seconds,
        badge=action.get('badge') or '定时关机',
        extra_text="取消关机",
        esc_hint="按 ESC 或点按钮会立即关机；要中止本次关机请点「取消关机」",
    )
    _shutdown_window.confirmed.connect(finish_shutdown)
    _shutdown_window.extra_clicked.connect(cancel_shutdown)


def preview_shutdown(rule=None):
    """预览定时关机窗口：按钮和 ESC 只关窗口，不会真的关机"""
    global _shutdown_window
    if _shutdown_window is not None:
        return
    rule = rule or shutdown_rule()
    action = (rule or {}).get('action') or {}
    seconds = shutdown_seconds(rule)
    win = ReminderWindow(
        action.get('title') or ((rule or {}).get('name') or '定时关机'),
        action.get('content', ''),
        hint=f"预览效果：按钮和 ESC 都只关闭预览，不会关机。实际会在 {seconds} 秒后关机。",
        button_text="关闭预览",
        countdown=seconds,
        badge="预览 · 定时关机",
        countdown_text="{n} 秒后自动关闭预览（不会关机）",
        esc_hint="按 ESC 或点击按钮关闭预览，不会关机",
    )
    win._preview = True
    win.confirmed.connect(_finish_shutdown_preview)
    _shutdown_window = win


def _finish_shutdown_preview():
    global _shutdown_window
    _shutdown_window = None


def finish_shutdown():
    """倒计时结束或点了「立即关机」：真正执行关机"""
    global _shutdown_window, _shutdown_rule
    _shutdown_window = None
    rule = _shutdown_rule
    _shutdown_rule = None
    name = (rule or {}).get('name') or '定时关机'
    if DRY_RUN:
        _log(f"DRY_RUN：已确认关机（{name}），不真正执行")
        return
    if not _enable_shutdown_privilege():
        _log("启用 SeShutdownPrivilege 失败")
    if user32.ExitWindowsEx(EWX_POWEROFF | EWX_FORCEIFHUNG, 0):
        _log(f"已发起关机（{name}）")
    else:
        _log(f"ExitWindowsEx 失败，错误码 {ctypes.get_last_error()}")


def cancel_shutdown():
    """取消定时关机：本次不再关机（若系统已在关机流程里，尽量撤回）"""
    global _shutdown_window, _shutdown_rule
    _log("用户取消了定时关机")
    _shutdown_window = None
    _shutdown_rule = None
    if not _enable_shutdown_privilege():
        return
    try:
        advapi32.AbortSystemShutdownW(None)
    except Exception as e:
        _log(f"中止关机失败：{e}")


# -------------------- 规则调度 --------------------
def parse_hhmm(value):
    hh, mm = str(value).strip().split(':')
    return int(hh), int(mm)

def rule_times(rule):
    """规则里配置的所有时间点 [(hour, minute), ...]"""
    out = []
    for item in (rule.get('trigger') or {}).get('times') or []:
        try:
            out.append(parse_hhmm(item))
        except Exception:
            continue
    return sorted(out)

def rule_weekdays(rule):
    """规则生效的星期：每日=7 天、工作日=周一到周五、自定义=勾选的天"""
    repeat = trigger_repeat(rule.get('trigger'))
    if repeat == REPEAT_WORKDAY:
        return list(WORKDAYS)
    if repeat == REPEAT_CUSTOM:
        return normalize_days((rule.get('trigger') or {}).get('weekdays')) or list(range(7))
    return list(range(7))

def rule_window(rule):
    """生效时段 (start, end)，两端都是 (时, 分)；没设置返回 None"""
    window = (rule.get('trigger') or {}).get('window') or {}
    start = window.get('start')
    end = window.get('end')
    if not start or not end:
        return None
    try:
        return parse_hhmm(start), parse_hhmm(end)
    except Exception:
        return None


def within_window(rule, now=None):
    """当前时间是否落在规则的生效时段里；跨零点也支持，未设置时段则总是生效"""
    bounds = rule_window(rule)
    if bounds is None:
        return True
    now = now or datetime.now()
    current = now.hour * 60 + now.minute
    start = bounds[0][0] * 60 + bounds[0][1]
    end = bounds[1][0] * 60 + bounds[1][1]
    if start == end:
        return True
    if start < end:
        return start <= current < end
    return current >= start or current < end


def window_text(rule):
    bounds = rule_window(rule)
    if bounds is None:
        return ''
    return "%02d:%02d–%02d:%02d" % (bounds[0][0], bounds[0][1], bounds[1][0], bounds[1][1])


def next_fire_time(rule, now=None):
    """该规则下一次触发时间；开机/关机类规则返回 None"""
    trig = rule.get('trigger') or {}
    if trigger_kind(trig) != TRIGGER_SCHEDULE:
        return None
    times_list = rule_times(rule)
    if not times_list:
        return None
    now = now or datetime.now()
    weekdays = rule_weekdays(rule)
    for offset in range(0, 9):
        day = (now + timedelta(days=offset)).date()
        if day.weekday() not in weekdays:
            continue
        for hh, mm in times_list:
            target = datetime(day.year, day.month, day.day, hh, mm, 0)
            if target > now:
                return target
    return None

def next_fire_overall():
    best = None
    for rule in rules:
        if not rule.get('enabled', True):
            continue
        target = next_fire_time(rule)
        if target is not None and (best is None or target < best):
            best = target
    return best

def schedule_rules():
    """所有规则共用一个单次定时器，每次重排到最近的一次触发"""
    global rule_timer
    if rule_timer is not None:
        rule_timer.stop()
        rule_timer = None
    target = next_fire_overall()
    if target is None:
        return
    delay = max(0.0, (target - datetime.now()).total_seconds())
    rule_timer = QTimer()
    rule_timer.setSingleShot(True)
    rule_timer.timeout.connect(check_due_rules)
    # 多等半秒，确保触发时已经进入目标分钟
    rule_timer.start(int(delay * 1000) + 500)

def check_due_rules():
    """到点：执行所有命中当前分钟的规则，再排下一次"""
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M")
    fired = False
    for rule in list(rules):
        trig = rule.get('trigger') or {}
        if not rule.get('enabled', True) or trigger_kind(trig) != TRIGGER_SCHEDULE:
            continue
        if now.weekday() not in rule_weekdays(rule):
            continue
        if (now.hour, now.minute) not in rule_times(rule):
            continue
        # 到点但不在生效时段内：这次就算了，等下一条
        if not within_window(rule, now):
            continue
        key = f"{rule.get('id')}@{stamp}"
        if key in reminded_dates:
            continue
        reminded_dates[key] = True
        fired = True
        execute_rule(rule)
    if fired:
        prune_fired_logs(now)
    schedule_rules()

def prune_fired_logs(now):
    """去重记录只留当天的，避免长期运行后无限增长"""
    today = now.strftime("%Y-%m-%d")
    for key in [k for k in list(reminded_dates) if today not in k]:
        reminded_dates.pop(key, None)

def execute_rule(rule):
    """执行规则动作：弹窗提醒或发起关机"""
    action = rule.get('action') or {}
    if action.get('type') == ACTION_SHUTDOWN:
        trigger_shutdown(rule)
    else:
        show_reminder(rule)

def next_rule_summary():
    """托盘菜单里显示的“下一次”提示"""
    best = None
    for rule in rules:
        if not rule.get('enabled', True):
            continue
        target = next_fire_time(rule)
        if target is not None and (best is None or target < best[0]):
            best = (target, rule)
    if best is None:
        return ''
    target, rule = best
    return f"下次 {target:%H:%M} · {rule.get('name', '')}"

# -------------------- 设置界面 --------------------
_DIALOG_TEMPLATE = """
QDialog { background: transparent; }
QFrame#windowCard { background: @dialog_bg; border: 1px solid @panel_border; border-radius: 16px; }
QFrame#windowHeader, QFrame#windowFooter { background: transparent; }
QPushButton#winClose { background: transparent; border: none; color: @text_muted;
                       font-size: 15px; padding: 0px; border-radius: 9px; }
QPushButton#winClose:hover { background: @danger_bg; color: @danger_text; }
QLabel#fieldLabel { color: @text_muted; font-size: 13px; }
QLabel#message { color: @text; font-size: 14px; }
QToolTip { color: @text; background: @menu_bg; border: 1px solid @menu_border; padding: 4px 8px; }
QLabel#inlineHint { color: @text_dim; font-size: 13px; }
QLabel#clockBar { color: @text; background: @input_bg;
                  border: 1px solid @input_border; border-radius: 10px;
                  padding: 7px 14px; font-size: 13px; }
QToolButton#timePick { background: transparent; border: none; color: @text_muted;
                       font-size: 15px; padding: 0px 2px; min-width: 16px; border-radius: 6px; }
QToolButton#timePick:hover { color: @countdown; background: @hover_bg; }
QToolButton#timePick:hover { color: @countdown; }
QListWidget#timeColumn { background: transparent; border: none; outline: 0;
                         color: @text; font-size: 13px; }
QListWidget#timeColumn::item { padding: 4px 8px; border-radius: 6px; }
QListWidget#timeColumn::item:hover { background: @hover_bg; }
QListWidget#timeColumn::item:selected { background: @accent_fill; color: @text_strong; }
QLabel { color: @text; }
QLabel#h1 { color: @text_strong; font-size: 21px; font-weight: 600; }
QLabel#sub { color: @text_muted; font-size: 13px; }
QLabel#sectionTitle { color: @text_strong; font-size: 14px; font-weight: 600; }
QLabel#hint { color: @text_muted; font-size: 13px; }
QLabel#versionValue { color: @text_strong; font-size: 15px; font-weight: 600; }
QLabel#startupHint { color: @text_muted; font-size: 13px; }
QLabel#startupHint[warn="true"] { color: @warn_text; }
QLabel#preview { color: @countdown; font-size: 14px; padding: 10px 12px;
                 background: @accent_soft; border-radius: 8px; }
QFrame#panel { background: @panel_bg; border: 1px solid @panel_border; border-radius: 14px; }
QFrame#warnBar { background: @accent_soft; border: 1px solid @accent_border; border-radius: 10px; }
QLabel#warnText { color: @warn_text; font-size: 13px; }
QFrame#tipBar { background: @success_bg; border: 1px solid @success_border; border-radius: 10px; }
QLabel#tipText { color: @success_text; font-size: 13px; }
QFrame#ruleCard { background: @card2_bg; border: 1px solid @card2_border; border-radius: 10px; }
QFrame#ruleCard:hover { background: @hover_bg; border-color: @accent_border; }
QLabel#catCount { color: @text_dim; font-size: 13px; background: @input_bg;
                  border: 1px solid @input_border; border-radius: 9px; padding: 1px 8px; }
QLabel#stateChip { font-size: 13px; border-radius: 10px; padding: 2px 10px; }
QLabel#stateChip[state="on"] { background: @chip_on_bg; color: @chip_on_fg;
                               border: 1px solid @chip_on_border; }
QLabel#stateChip[state="off"] { background: @chip_off_bg; color: @chip_off_fg;
                                border: 1px solid @chip_off_border; }
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {
    background: @input_bg; border: 1px solid @input_border;
    border-radius: 8px; padding: 6px 10px; color: @input_text;
    selection-background-color: @accent; selection-color: @on_accent;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {
    border: 1px solid @accent_border_strong;
}
QComboBox { padding-right: 26px; }
QComboBox::drop-down { border: none; width: 24px; subcontrol-position: center right; }
QComboBox QAbstractItemView { background: @menu_bg; color: @menu_text; border: 1px solid @menu_border;
                              border-radius: 10px; padding: 6px; outline: 0;
                              selection-background-color: transparent; }
QComboBox QAbstractItemView::item { min-height: 26px; padding: 4px 12px; border-radius: 6px; }
QComboBox QAbstractItemView::item:hover { background: @hover_bg; }
QComboBox QAbstractItemView::item:selected { background: @accent_fill; color: @text_strong; }
QTableWidget { background: transparent; border: none; gridline-color: @panel_border;
               color: @text; selection-background-color: @accent_soft; }
QTableWidget::item { padding: 6px; }
QHeaderView::section { background: @input_bg; color: @text_muted; border: none; padding: 8px; }
QPushButton { background: @btn_bg; border: 1px solid @btn_border;
              border-radius: 8px; padding: 7px 16px; color: @text; }
QPushButton:hover { background: @hover_bg; border-color: @accent_border; }
QPushButton:pressed { background: @card2_bg; }
QPushButton#primary { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 @accent2, stop:1 @accent);
                      border: none; color: @on_accent; font-weight: 600; padding: 8px 22px; }
QPushButton#primary:hover { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 @accent2_hover, stop:1 @accent_hover); }
QPushButton#primary:pressed { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 @accent2_pressed, stop:1 @accent_pressed); }
QPushButton#link { background: transparent; border: none; padding: 5px 9px; color: @text_muted; }
QPushButton#link:hover { color: @countdown; background: @hover_bg; border-radius: 6px; }
QPushButton#link:pressed { background: @input_bg; }
QPushButton#danger { background: @danger_bg; border: 1px solid @danger_border;
                     color: @danger_text; border-radius: 8px; padding: 7px 16px; }
QPushButton#danger:hover { background: @danger_hover; }
QCheckBox { color: @text; spacing: 8px; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px;
                       border: 1px solid @input_border; background: @input_bg; }
QCheckBox::indicator:checked { background: @accent; border-color: @accent; }
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 6px 2px 6px 0; }
QScrollBar::handle:vertical { background: @scrollbar; border-radius: 4px; min-height: 40px; }
QScrollBar::handle:vertical:hover { background: @accent_border_strong; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; border: none; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
"""

_MENU_TEMPLATE = """
QMenu { background: transparent; border: none; padding: 6px; font-size: 13px; }
QMenu::item { padding: 5px 18px 5px 12px; border-radius: 6px; color: @menu_text; }
QMenu::item:selected { background: @menu_sel; }
QMenu::item:disabled { color: @text_dim; }
QMenu::separator { height: 1px; background: @menu_border; margin: 4px 8px; }
QMenu::indicator { width: 10px; }
"""


def font_scale():
    """界面字号倍率：高分屏或小屏上字太小看不清时，用户可在设置里调大"""
    try:
        scale = float((options.get('display') or {}).get('font_scale') or 1.0)
    except (TypeError, ValueError):
        scale = 1.0
    return min(1.6, max(0.9, scale))


def _scale_qss_fonts(qss, factor):
    """只放大字号，不动间距和尺寸，避免把布局撑破"""
    if abs(factor - 1.0) < 1e-6:
        return qss

    def repl(match):
        size = int(match.group(1))
        return 'font-size: %dpx' % max(10, int(round(size * factor)))

    return re.sub(r'font-size:\s*(\d+)px', repl, qss)


def _render_qss(template, tokens):
    """把 @token 替换成主题里的颜色；长键先替换，避免 accent 吃掉 accent2"""
    text = _scale_qss_fonts(template, font_scale())
    for key in sorted(tokens, key=len, reverse=True):
        value = tokens[key]
        if isinstance(value, (str, int, float)):
            text = text.replace('@' + key, str(value))
    return text


def dialog_qss():
    tokens = theme()
    # 状态胶囊：不同状态给不同的底色/描边，跟随主题而不是写死颜色
    tokens['chip_on_bg'] = rgba_of(tokens['success_text'], 0.18)
    tokens['chip_on_fg'] = tokens['success_text']
    tokens['chip_on_border'] = rgba_of(tokens['success_text'], 0.42)
    tokens['chip_off_bg'] = rgba_of(tokens['accent'], 0.16)
    tokens['chip_off_fg'] = tokens['warn_text']
    tokens['chip_off_border'] = rgba_of(tokens['accent'], 0.38)
    return _render_qss(_DIALOG_TEMPLATE, tokens)


def menu_qss():
    return _render_qss(_MENU_TEMPLATE, theme())


def apply_theme_everywhere():
    """主题改动后同步已打开的设置窗口、托盘菜单与托盘图标"""
    if _settings_window is not None:
        _settings_window.setStyleSheet(dialog_qss() + _indicator_qss())
        _settings_window.reload()
    menu_obj = globals().get('menu')
    if menu_obj is not None:
        menu_obj.setStyleSheet(menu_qss())
        update_menu()
    tray_obj = globals().get('tray')
    if tray_obj is not None:
        tray_obj.setIcon(create_icon())


_settings_window = None

def describe_trigger(rule):
    """把「类别 + 时间」翻译成一句人话，给首页卡片用"""
    trig = rule.get('trigger') or {}
    kind = trigger_kind(trig)
    repeat = trigger_repeat(trig)
    days = rule_weekdays(rule)
    span = window_text(rule)

    if kind == TRIGGER_BOOT:
        if repeat == REPEAT_WORKDAY:
            text = "开机启动（工作日）"
        elif repeat == REPEAT_CUSTOM:
            text = "开机启动（每周%s）" % "、".join(WEEKDAY_CN[d] for d in days)
        else:
            text = "开机启动（每天）"
        return f"{text} · {span} 内有效" if span else text

    times_text = "、".join(f"{h:02d}:{m:02d}" for h, m in rule_times(rule)) or "未设置时间"
    if repeat == REPEAT_WORKDAY:
        text = f"工作日 {times_text}"
    elif repeat == REPEAT_CUSTOM:
        text = "每周%s %s" % ("、".join(WEEKDAY_CN[d] for d in days), times_text)
    else:
        text = f"每天 {times_text}"
    return f"{text}（{span} 内有效）" if span else text

def describe_action(rule):
    action = rule.get('action') or {}
    if action.get('type') == ACTION_SHUTDOWN:
        return "关机"
    return "提醒"

def _times_text(rule):
    return ", ".join(f"{h:02d}:{m:02d}" for h, m in rule_times(rule))

_indicator_cache = {}


def _indicator_qss():
    """勾选图标、下拉箭头在运行时画好再交给样式表（Qt 样式表画不出对勾和细箭头）"""
    tokens = theme()
    accent = tokens.get('accent') or COLOR_LUOXIA
    key = (tokens.get('name'), accent, tokens.get('text_muted'))
    if key in _indicator_cache:
        return _indicator_cache[key]

    folder = os.path.join(tempfile.gettempdir(), 'warningwork_ui')
    os.makedirs(folder, exist_ok=True)
    suffix = '%s_%s' % (key[0], accent.lstrip('#') or 'auto')
    on_path = os.path.join(folder, 'check_on_%s.png' % suffix)
    off_path = os.path.join(folder, 'check_off_%s.png' % suffix)
    arrow_path = os.path.join(folder, 'chevron_%s.png' % suffix)

    for path, checked in ((on_path, True), (off_path, False)):
        pixmap = QPixmap(36, 36)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        if checked:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(accent))
            painter.drawRoundedRect(4, 4, 28, 28, 8, 8)
            pen = QPen(QColor(255, 255, 255), 3.6)
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            painter.drawLine(QPointF(11, 18.5), QPointF(16, 23.5))
            painter.drawLine(QPointF(16, 23.5), QPointF(25, 12.5))
        else:
            painter.setPen(QPen(QColor(255, 255, 255, 80), 1.8))
            painter.setBrush(QColor(255, 255, 255, 14))
            painter.drawRoundedRect(4, 4, 28, 28, 8, 8)
        painter.end()
        pixmap.save(path)

    # 下拉箭头：两根圆头短线，颜色跟随主题弱化文字色
    arrow = QPixmap(28, 28)
    arrow.fill(Qt.transparent)
    painter = QPainter(arrow)
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(tokens.get('text_muted') or '#8f9bb3'), 2.4)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.drawLine(QPointF(8.5, 11), QPointF(14, 16.5))
    painter.drawLine(QPointF(14, 16.5), QPointF(19.5, 11))
    painter.end()
    arrow.save(arrow_path)

    on_url = on_path.replace('\\', '/')
    off_url = off_path.replace('\\', '/')
    arrow_url = arrow_path.replace('\\', '/')
    qss = ("QCheckBox::indicator{width:16px;height:16px;image:url(%s);}"
           "QCheckBox::indicator:checked{image:url(%s);}"
           "QTableView::indicator,QTableWidget::indicator{width:16px;height:16px;image:url(%s);}"
           "QTableView::indicator:checked,QTableWidget::indicator:checked{image:url(%s);}"
           "QComboBox::down-arrow{image:url(%s);width:12px;height:12px;}"
           % (off_url, on_url, off_url, on_url, arrow_url))
    _indicator_cache[key] = qss
    return qss


def preset_rule(key):
    """按预设 key 生成一条规则"""
    for preset in PRESETS:
        if preset.get('key') == key:
            return preset['make']()
    return None

def _css_color(value, fallback=None):
    """把主题里的 #hex / rgba(r, g, b, a%) 文本转成 QColor，供自绘用"""
    text = str(value or '').strip()
    if text.startswith('#'):
        color = QColor(text)
        if color.isValid():
            return color
    match = re.match(r'rgba?\(([^)]+)\)', text)
    if match:
        parts = [part.strip() for part in match.group(1).split(',')]
        try:
            red, green, blue = (int(float(part)) for part in parts[:3])
            alpha = 255
            if len(parts) > 3:
                raw = parts[3]
                if raw.endswith('%'):
                    alpha = int(round(float(raw[:-1]) * 2.55))
                else:
                    number = float(raw)
                    alpha = int(round(number * 255)) if number <= 1 else int(number)
            return QColor(red, green, blue, max(0, min(255, alpha)))
        except ValueError:
            pass
    return fallback if fallback is not None else QColor(255, 255, 255)


class ComboItemDelegate(QStyledItemDelegate):
    """下拉行高由委托决定，样式表里的 min-height 对弹出视图不生效"""

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        return QSize(size.width() + 26, max(34, size.height() + 10))


def combo_popup_qss():
    """下拉弹层自己的样式表：直接挂在视图上，避免弹出窗口不继承对话框样式"""
    tokens = theme()
    selected = (rgba_of(tokens['accent'], 0.28) if tokens['mode'] == 'light'
                else rgba_of(tokens['accent2'], 0.42))
    return (
        "QAbstractItemView{background:%s;color:%s;border:1px solid %s;"
        "border-radius:14px;padding:6px;outline:0;selection-background-color:transparent;}"
        "QAbstractItemView::item{min-height:32px;padding:0 12px;border-radius:8px;color:%s;}"
        "QAbstractItemView::item:hover{background:%s;}"
        "QAbstractItemView::item:selected{background:%s;color:%s;}"
        % (tokens['menu_bg'], tokens['menu_text'], tokens['menu_border'],
           tokens['menu_text'], tokens['hover_bg'], selected, tokens['text_strong']))


def apply_round_mask(widget, radius):
    """把窗口裁成圆角：不透明弹层光靠样式表画不出透明角，会露出黑底

    遮罩用整块 rect、不向内缩，否则 0.5px 的描边会被裁掉一半，
    表现为左边和上边的边框“缺失”。
    """
    path = QPainterPath()
    path.addRoundedRect(QRectF(widget.rect()), radius, radius)
    widget.setMask(QRegion(path.toFillPolygon().toPolygon()))


class RoundedPopupFilter(QObject):
    """弹层每次显示/改变尺寸都重新裁一次圆角"""

    def __init__(self, radius=14, parent=None):
        super().__init__(parent)
        self._radius = radius

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Show, QEvent.Resize):
            apply_round_mask(obj, self._radius)
        return False


def polish_combo(combo):
    """让下拉列表贴住输入框、四角圆润：弹出层去掉系统边框并允许透明"""
    view = combo.view()
    view.setFrameShape(QFrame.NoFrame)
    view.setStyleSheet(combo_popup_qss())
    view.setItemDelegate(ComboItemDelegate(view))
    view.setSpacing(2)
    view.setCursor(Qt.PointingHandCursor)
    view.viewport().setCursor(Qt.PointingHandCursor)
    popup = view.window()
    popup.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
    popup.setAttribute(Qt.WA_TranslucentBackground)
    popup.setStyleSheet("background: %s; border: none;" % theme()['menu_bg'])
    popup.setContentsMargins(0, 4, 0, 4)
    # 透明底在部分机器上会被画成黑块，这里用圆角遮罩裁掉四角，效果稳定
    popup_filter = RoundedPopupFilter(14, combo)
    popup.installEventFilter(popup_filter)
    combo._popup_filter = popup_filter
    combo.setCursor(Qt.PointingHandCursor)


# -------------------- 下拉浮层 --------------------

_POPUP_CLOSED_AT = 0.0
_ACTIVE_POPUP = None


def _close_active_popup():
    """同一时刻只允许一个浮层：打开新的先把旧的收掉"""
    global _ACTIVE_POPUP
    popup, _ACTIVE_POPUP = _ACTIVE_POPUP, None
    if popup is not None:
        try:
            if popup.isVisible():
                popup.close()
        except Exception:
            pass


def _remember_popup(popup):
    global _ACTIVE_POPUP
    _ACTIVE_POPUP = popup


def _mark_popup_closed():
    """记录弹层刚关闭的时刻，避免“关掉又立刻弹开”"""
    global _POPUP_CLOSED_AT
    _POPUP_CLOSED_AT = time.monotonic()


def _popup_just_closed(threshold=0.2):
    return time.monotonic() - _POPUP_CLOSED_AT < threshold


def combo_list_qss():
    """浮层列表样式：透明底，选中项用强调色填充"""
    tokens = theme()
    selected = (rgba_of(tokens['accent'], 0.28) if tokens['mode'] == 'light'
                else rgba_of(tokens['accent2'], 0.42))
    return (
        "QListWidget#comboList{background:transparent;border:none;outline:none;}"
        "QListWidget#comboList::item{height:32px;padding-left:10px;border-radius:8px;"
        "color:%s;}"
        "QListWidget#comboList::item:hover{background:%s;}"
        "QListWidget#comboList::item:selected{background:%s;color:%s;}"
        % (tokens['menu_text'], tokens['hover_bg'], selected, tokens['text_strong']))


class ComboPopup(QFrame):
    """自绘圆角浮层：系统气泡窗口在部分机器上四角会发黑，这里完全自己画"""

    picked = pyqtSignal(int)

    def __init__(self, parent=None, items=None, current=-1, width=140):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint
                         | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._tokens = theme()
        self._items = list(items or [])

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(0)
        self.view = QListWidget()
        self.view.setObjectName("comboList")
        self.view.setFrameShape(QFrame.NoFrame)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.view.setStyleSheet(combo_list_qss())
        for text in self._items:
            item = QListWidgetItem(text)
            item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            self.view.addItem(item)
        if 0 <= current < self.view.count():
            self.view.setCurrentRow(current)
            self.view.scrollToItem(self.view.item(current),
                                   QAbstractItemView.PositionAtCenter)
        self.view.itemClicked.connect(self._pick)
        root.addWidget(self.view)

        rows = min(max(1, len(self._items)), 8)
        self.view.setFixedHeight(32 * rows + 4)
        widest = max([len(text) for text in self._items] or [0])
        self.setFixedWidth(max(160, min(int(width), 340), widest * 17 + 30))
        apply_round_mask(self, 12)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        # 向内留 1px，让描边完整落在遮罩内，四边都不会被裁掉
        rect = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
        path.addRoundedRect(rect, 11, 11)
        painter.fillPath(path, QColor(_css_color(self._tokens['menu_bg'], '#ffffff')))
        # 边框比面板稍深一点，四边都要看得见，不然会像是“被裁掉”
        edge = ('rgba(23, 33, 54, 22%)' if self._tokens['mode'] == 'light'
                else 'rgba(255, 255, 255, 16%)')
        painter.setPen(QPen(QColor(_css_color(edge, '#d8d8d8')), 1))
        painter.drawPath(path)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        apply_round_mask(self, 12)

    def showEvent(self, event):
        super().showEvent(event)
        apply_round_mask(self, 12)

    def closeEvent(self, event):
        _mark_popup_closed()
        super().closeEvent(event)

    def _pick(self, item):
        self.picked.emit(self.view.row(item))
        self.close()


def _card_combo_show_popup(self):
    """顶掉 QComboBox 的系统弹层：改成自己画的圆角浮层"""
    if _popup_just_closed():
        return
    popup = getattr(self, '_card_popup', None)
    if popup is not None and popup.isVisible():
        popup.close()
        return
    items = [self.itemText(i) for i in range(self.count())]
    if not items:
        return
    _close_active_popup()
    popup = ComboPopup(self.window(), items, self.currentIndex(), self.width())
    popup.picked.connect(self.setCurrentIndex)
    self._card_popup = popup
    _remember_popup(popup)
    popup.adjustSize()

    below = self.mapToGlobal(QPoint(0, self.height() + 2))
    screen = QApplication.screenAt(below) or QApplication.primaryScreen()
    area = screen.availableGeometry()
    x = min(max(below.x(), area.left()), area.right() - popup.width())
    y = below.y()
    if y + popup.height() > area.bottom():
        above = self.mapToGlobal(QPoint(0, 0)).y() - popup.height() - 2
        y = max(area.top(), above)
    popup.move(x, y)
    popup.show()


QComboBox.showPopup = _card_combo_show_popup


class CardDialog(QDialog):
    """统一的无边框圆角窗口：可拖动标题栏、内容超高自动滚动、底部按钮区"""

    def __init__(self, parent=None, title="", subtitle="", width=680,
                 auto_fit=True, use_scroll=True, modal=True):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        # 用 WindowModal 而不是 ApplicationModal：弹窗只挡住它的父窗口，
        # 全屏提醒、托盘消息这类独立窗口仍能接收点击（否则提醒窗口会点不动）。
        self.setWindowModality(Qt.WindowModal if modal else Qt.NonModal)
        # 初始宽度只是首选值；实际下限由内容决定（见 showEvent）
        self._preferred_width = width
        self.setMinimumWidth(min(width, 620))
        self._auto_fit = auto_fit
        self._fitted = False
        self._drag_offset = None
        self._resize_edges = None
        self._resize_origin = None
        self._resize_geo = None
        self.setMouseTracking(True)

        shell = QVBoxLayout(self)
        shell.setContentsMargins(16, 10, 16, 16)
        shell.setSpacing(0)

        self.card = QFrame()
        self.card.setObjectName("windowCard")
        glow = QGraphicsDropShadowEffect(self.card)
        # 阴影只提示“浮在桌面上”即可，太宽会显得脏
        glow.setBlurRadius(20)
        glow.setOffset(0, 6)
        glow.setColor(QColor(0, 0, 0, 120))
        self.card.setGraphicsEffect(glow)
        shell.addWidget(self.card)

        card_box = QVBoxLayout(self.card)
        card_box.setContentsMargins(0, 0, 0, 0)
        card_box.setSpacing(0)

        self.header = QFrame()
        self.header.setObjectName("windowHeader")
        head = QHBoxLayout(self.header)
        head.setContentsMargins(24, 20, 14, 14)
        head.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(7)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("h1")
        titles.addWidget(self.title_label)
        self.sub_label = QLabel(subtitle, self.header)
        self.sub_label.setObjectName("sub")
        self.sub_label.setWordWrap(True)
        titles.addWidget(self.sub_label)
        # 必须先挂进布局（有 parent）再决定显示，否则 setVisible(True) 会让它
        # 以顶层窗口的身份单独闪一下（用户反馈的“点设置闪现小窗口”）
        self.sub_label.setVisible(bool(subtitle))
        head.addLayout(titles, 1)
        self.header_extra = QHBoxLayout()
        self.header_extra.setSpacing(8)
        head.addLayout(self.header_extra)
        self.close_btn = QPushButton("✕")
        self.close_btn.setObjectName("winClose")
        self.close_btn.setFixedSize(28, 28)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.clicked.connect(self.reject)
        head.addWidget(self.close_btn, 0, Qt.AlignTop)
        card_box.addWidget(self.header)

        if use_scroll:
            self.scroll = QScrollArea()
            self.scroll.setWidgetResizable(True)
            self.scroll.setFrameShape(QFrame.NoFrame)
            self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.scroll.viewport().setAutoFillBackground(False)
            content = QWidget()
            content.setAutoFillBackground(False)
            self.body = QVBoxLayout(content)
            self.body.setContentsMargins(22, 2, 22, 16)
            self.body.setSpacing(12)
            self.scroll.setWidget(content)
            card_box.addWidget(self.scroll, 1)
            self._content = content
        else:
            self.scroll = None
            holder = QWidget()
            holder.setAutoFillBackground(False)
            self.body = QVBoxLayout(holder)
            self.body.setContentsMargins(24, 2, 24, 6)
            self.body.setSpacing(12)
            card_box.addWidget(holder, 1)
            self._content = holder

        self.footer_frame = QFrame()
        self.footer_frame.setObjectName("windowFooter")
        self.footer = QHBoxLayout(self.footer_frame)
        self.footer.setContentsMargins(24, 10, 24, 18)
        self.footer.setSpacing(10)
        self.footer.addStretch(1)
        card_box.addWidget(self.footer_frame)

        for widget in (self, self.card, self.header, self.title_label, self.sub_label):
            widget.setMouseTracking(True)
            widget.installEventFilter(self)

    # ---------- 拉伸与拖动 ----------
    RESIZE_BAND = 9
    MIN_HEIGHT = 300

    def _card_rect(self):
        """卡片在对话框坐标里的矩形（卡片可能被布局/容器包着，不能直接用 geometry）"""
        return QRect(self.card.mapTo(self, QPoint(0, 0)), self.card.size())

    def _header_rect(self):
        return QRect(self.header.mapTo(self, QPoint(0, 0)), self.header.size())

    def _edges_at(self, pos):
        card = self._card_rect()
        band = self.RESIZE_BAND
        return (abs(pos.x() - card.left()) <= band,
                abs(pos.x() - card.right()) <= band,
                abs(pos.y() - card.top()) <= band,
                abs(pos.y() - card.bottom()) <= band)

    @staticmethod
    def _cursor_for_edges(edges):
        left, right, top, bottom = edges
        if (left and top) or (right and bottom):
            return Qt.SizeFDiagCursor
        if (right and top) or (left and bottom):
            return Qt.SizeBDiagCursor
        if left or right:
            return Qt.SizeHorCursor
        if top or bottom:
            return Qt.SizeVerCursor
        return Qt.ArrowCursor

    def _hover_cursor(self, pos):
        self.setCursor(self._cursor_for_edges(self._edges_at(pos)))

    def _begin_resize(self, pos, global_pos):
        edges = self._edges_at(pos)
        if not any(edges):
            return False
        self._resize_edges = edges
        self._resize_origin = global_pos
        self._resize_geo = QRect(self.geometry())
        self.setCursor(self._cursor_for_edges(edges))
        return True

    def _apply_resize(self, global_pos):
        if self._resize_edges is None:
            return
        left, right, top, bottom = self._resize_edges
        dx = global_pos.x() - self._resize_origin.x()
        dy = global_pos.y() - self._resize_origin.y()
        geometry = QRect(self._resize_geo)
        min_width = max(self.minimumWidth(), 360)
        min_height = self.MIN_HEIGHT
        if left:
            geometry.setLeft(min(geometry.left() + dx, geometry.right() - min_width))
        if right:
            geometry.setRight(max(geometry.right() + dx, geometry.left() + min_width))
        if top:
            geometry.setTop(min(geometry.top() + dy, geometry.bottom() - min_height))
        if bottom:
            geometry.setBottom(max(geometry.bottom() + dy, geometry.top() + min_height))
        self.setGeometry(geometry)

    # 这些控件上的鼠标事件都要参与“贴边缩放/拖动标题栏”的判定
    def _tracked(self, obj):
        return obj in (self, self.card, self.header, self.title_label, self.sub_label)

    def eventFilter(self, obj, event):
        if not self._tracked(obj):
            return super().eventFilter(obj, event)
        etype = event.type()

        if etype == QEvent.Leave:
            if self._resize_edges is None and self._drag_offset is None:
                self.setCursor(Qt.ArrowCursor)
            return False

        # 只有鼠标事件才有 pos()，其余事件直接放行
        if not hasattr(event, 'pos'):
            return super().eventFilter(obj, event)
        local = event.pos() if obj is self else obj.mapTo(self, event.pos())

        if etype in (QEvent.MouseMove, QEvent.HoverMove):
            if self._resize_edges is not None:
                self._apply_resize(event.globalPos())
                return True
            if self._drag_offset is not None:
                self.move(event.globalPos() - self._drag_offset)
                return True
            self._hover_cursor(local)
            return False

        if etype == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            if self._begin_resize(local, event.globalPos()):
                return True
            if self._header_rect().contains(local):
                self._drag_offset = event.globalPos() - self.frameGeometry().topLeft()
                return True
            return False

        if etype == QEvent.MouseButtonRelease:
            if self._resize_edges is not None or self._drag_offset is not None:
                self._resize_edges = None
                self._drag_offset = None
                self.setCursor(Qt.ArrowCursor)
                return True
        return super().eventFilter(obj, event)

    # ---------- 尺寸：比屏幕高时改为滚动 ----------
    def showEvent(self, event):
        super().showEvent(event)
        translate = globals().get('translate_widgets')
        if translate is not None:
            translate(self)
        self._sync_field_labels()
        if not self._auto_fit or self._fitted:
            return
        self._fitted = True
        screen = QApplication.primaryScreen().availableGeometry()
        content_height = 0
        content_width = 0
        spacing = self.body.spacing()
        for index in range(self.body.count()):
            item = self.body.itemAt(index)
            if item.widget() is not None:
                content_height += item.widget().sizeHint().height()
                content_width = max(content_width, item.widget().sizeHint().width())
            elif item.layout() is not None:
                content_height += item.layout().sizeHint().height()
                content_width = max(content_width, item.layout().sizeHint().width())
        content_height += spacing * max(0, self.body.count() - 1)
        content_height += 24
        margins = self.body.contentsMargins()
        content_width += margins.left() + margins.right()
        # 最小宽度按“内容能完整显示”来定，避免右侧被裁掉
        need_width = max(content_width,
                         self.header.sizeHint().width(),
                         self.footer_frame.sizeHint().width()) + 32
        limit = screen.width() - 40
        self.setMinimumWidth(min(need_width, limit))
        width = min(max(getattr(self, '_preferred_width', need_width), need_width), limit)
        apply_translation = globals().get('translate_widgets')
        if apply_translation is not None:
            apply_translation(self)
        height = (content_height + self.header.sizeHint().height()
                  + self.footer_frame.sizeHint().height() + 24 + 24 + 16 + 10)
        height = min(height, int(screen.height() * 0.8))
        height = max(height, 280)
        self.resize(width, height)
        self.move(screen.center().x() - width // 2,
                  screen.center().y() - height // 2)

    # ---------- 内容搭件 ----------
    def add_section(self, title):
        """一块带标题的面板，返回可以在里面继续加行的布局"""
        panel = QFrame()
        panel.setObjectName("panel")
        box = QVBoxLayout(panel)
        box.setContentsMargins(16, 12, 16, 14)
        box.setSpacing(10)
        label = QLabel(title)
        label.setObjectName("sectionTitle")
        box.addWidget(label)
        self.body.addWidget(panel)
        return box

    def _sync_field_labels(self):
        """标签列按当前语言里最长的那一个统一宽度，避免英文被裁掉"""
        labels = [w for w in self.findChildren(QLabel) if w.objectName() == "fieldLabel"]
        if not labels:
            return
        width = max(64, max(w.sizeHint().width() for w in labels) + 6)
        for label in labels:
            label.setFixedWidth(width)

    def field_row(self, label, field, compact=False):
        """固定宽度标签 + 输入控件的一行；compact=True 时控件按自身宽度靠左，不拉伸"""
        row = QWidget()
        row.setAutoFillBackground(False)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        tag = QLabel(label)
        tag.setObjectName("fieldLabel")
        tag.setMinimumWidth(64)
        tag.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(tag)
        if compact:
            layout.addWidget(field)
            layout.addStretch(1)
        else:
            layout.addWidget(field, 1)
        return row

    def make_button(self, text, primary=False):
        button = QPushButton(text)
        if primary:
            button.setObjectName("primary")
        button.setCursor(Qt.PointingHandCursor)
        return button


class RoundMenu(QMenu):
    """托盘菜单：QSS 的圆角不会裁剪窗口，这里自绘圆角底板"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint
                            | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)

    def paintEvent(self, event):
        tokens = theme()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)
        painter.fillPath(path, _css_color(tokens['menu_bg']))
        pen = QPen(_css_color(tokens['menu_border']))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)
        painter.end()
        super().paintEvent(event)


class TimePickerPopup(QFrame):
    """左“时”右“分”的滚动选择器：滚轮或点选，选完自动落值"""

    picked = pyqtSignal(str)

    def __init__(self, parent=None, value=''):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint
                         | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        # 不抢焦点：输入框保持光标，边打字边点选都行
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.NoFocus)
        self.setStyleSheet(dialog_qss() + _indicator_qss())

        hour, minute = 8, 0
        try:
            if value:
                hour, minute = parse_hhmm(value)
        except Exception:
            pass
        self._hour, self._minute = hour, minute

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(6)
        for text in ("时", "分"):
            label = QLabel(text)
            label.setObjectName("fieldLabel")
            label.setAlignment(Qt.AlignCenter)
            head.addWidget(label)
        root.addLayout(head)

        body = QHBoxLayout()
        body.setSpacing(6)
        self.hour_list = self._column([f"{h:02d}" for h in range(24)], hour)
        self.minute_list = self._column([f"{m:02d}" for m in range(60)], minute)
        body.addWidget(self.hour_list, 1)
        body.addWidget(self.minute_list, 1)
        root.addLayout(body)

        self.hour_list.itemClicked.connect(self._pick_hour)
        self.minute_list.itemClicked.connect(self._pick_minute)

        confirm = QPushButton("确定")
        confirm.setObjectName("primary")
        confirm.clicked.connect(self._commit)
        root.addWidget(confirm)
        self.setMinimumWidth(180)

    def fit_width(self, width):
        """弹层宽度跟输入框对齐（太窄时留出可读下限）"""
        self.setFixedWidth(max(int(width), 180))

    def _column(self, values, current):
        view = QListWidget()
        view.setObjectName("timeColumn")
        view.setMinimumWidth(72)
        view.setMinimumHeight(168)
        view.setFrameShape(QFrame.NoFrame)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        for text in values:
            item = QListWidgetItem(text)
            item.setTextAlignment(Qt.AlignCenter)
            view.addItem(item)
        view.setCurrentRow(current)
        view.scrollToItem(view.item(current), QAbstractItemView.PositionAtCenter)
        return view

    def _pick_hour(self, item):
        self._hour = self.hour_list.row(item)

    def _pick_minute(self, item):
        self._minute = self.minute_list.row(item)
        self._commit()

    def _commit(self):
        self.picked.emit("%02d:%02d" % (self._hour, self._minute))
        self.close()

    def closeEvent(self, event):
        _mark_popup_closed()
        super().closeEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter):
            if event.key() != Qt.Key_Escape:
                self._commit()
                return
            self.close()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):
        tokens = theme()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)
        painter.fillPath(path, _css_color(tokens['menu_bg']))
        pen = QPen(_css_color(tokens['menu_border']))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)
        painter.end()
        super().paintEvent(event)


def _theme_chevron_path():
    """当前主题的下拉箭头图标路径（由 _indicator_qss 生成）"""
    tokens = theme()
    accent = tokens.get('accent') or COLOR_LUOXIA
    folder = os.path.join(tempfile.gettempdir(), 'warningwork_ui')
    path = os.path.join(folder, 'chevron_%s_%s.png' % (
        tokens.get('name'), accent.lstrip('#') or 'auto'))
    if not os.path.exists(path):
        _indicator_qss()
    return path


class TimeField(QLineEdit):
    """时间输入框：点一下就地展开时分选择器，选完自动落值

    只允许通过选择器取值，不开放手动输入（以后加搜索再放开）。
    """

    def __init__(self, text='', parent=None, multiple=False):
        super().__init__(text, parent)
        self._multiple = multiple
        self._popup = None
        self.setClearButtonEnabled(False)
        self.setTextMargins(0, 0, 22, 0)
        self.setReadOnly(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("点一下选择时间")

    # ---- 绘制右侧箭头 ----
    def paintEvent(self, event):
        super().paintEvent(event)
        try:
            pixmap = QPixmap(_theme_chevron_path())
        except Exception:
            return
        if pixmap.isNull():
            return
        painter = QPainter(self)
        target = QRect(self.width() - 22, (self.height() - 12) // 2, 12, 12)
        painter.drawPixmap(target, pixmap.scaled(12, 12, Qt.KeepAspectRatio,
                                                 Qt.SmoothTransformation))
        painter.end()

    # ---- 点击即展开 ----
    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        self.open_picker()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Down, Qt.Key_F4):
            self.open_picker()
            return
        if event.key() == Qt.Key_Escape and self._popup is not None:
            self.close_picker()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event):
        self.close_picker()
        super().hideEvent(event)

    # ---- 浮层 ----
    def open_picker(self):
        if self._popup is not None and self._popup.isVisible():
            return
        if _popup_just_closed():
            return
        _close_active_popup()
        popup = TimePickerPopup(self.window(), self._current_value())
        _remember_popup(popup)
        popup.picked.connect(self._apply)
        popup.fit_width(self.width())
        popup.adjustSize()
        anchor = self.mapToGlobal(QPoint(0, self.height() + 4))
        screen = QApplication.primaryScreen().availableGeometry()
        if anchor.y() + popup.height() > screen.bottom():
            anchor.setY(self.mapToGlobal(QPoint(0, 0)).y() - popup.height() - 4)
        if anchor.x() + popup.width() > screen.right():
            anchor.setX(max(screen.left() + 8, screen.right() - popup.width() - 8))
        popup.setGeometry(anchor.x(), anchor.y(), popup.width(), popup.height())
        popup.winId()  # 先建好原生窗口，避免第一帧闪在屏幕左上角
        popup.destroyed.connect(self._forget_popup)
        popup.show()
        self._popup = popup
        QApplication.instance().installEventFilter(self)
        self.setFocus()

    def close_picker(self):
        popup, self._popup = self._popup, None
        if popup is not None:
            try:
                popup.close()
                popup.deleteLater()
            except Exception:
                pass
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

    def _forget_popup(self, *_):
        self._popup = None
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

    def eventFilter(self, obj, event):
        """点别处就收起浮层"""
        if self._popup is not None and event.type() == QEvent.MouseButtonPress:
            point = event.globalPos()
            inside_self = self.rect().contains(self.mapFromGlobal(point))
            inside_popup = self._popup.rect().contains(self._popup.mapFromGlobal(point))
            if not inside_self and not inside_popup:
                self.close_picker()
        return super().eventFilter(obj, event)

    # ---- 取值 / 落值 ----
    def _segments(self):
        return [part.strip() for part in self.text().replace('，', ',').split(',')]

    def _segment_index(self):
        prefix = self.text()[:self.cursorPosition()].replace('，', ',')
        return prefix.count(',')

    def _current_value(self):
        parts = self._segments()
        index = min(self._segment_index(), len(parts) - 1) if parts else 0
        value = parts[index] if parts and parts[index] else ''
        try:
            parse_hhmm(value)
            return value
        except Exception:
            return ''

    def _apply(self, value):
        parts = [part for part in self._segments() if part]
        if not self._multiple or len(parts) <= 1:
            self.setText(value)
        else:
            index = min(self._segment_index(), len(parts) - 1)
            parts[index] = value
            self.setText('，'.join(parts))
        self.setFocus()
        self.selectAll()
        self.close_picker()


class MessageDialog(CardDialog):
    """统一的提示/确认窗口：跟着主题走，替代系统 QMessageBox"""

    def __init__(self, parent=None, title="", message="", ok_text="确定",
                 cancel_text=None, danger=False):
        super().__init__(parent, title, "", width=420)
        self.setWindowTitle(title)
        label = QLabel(message)
        label.setObjectName("message")
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.addWidget(label)
        ok = self.make_button(ok_text, primary=not danger)
        if danger:
            ok.setObjectName("danger")
        ok.clicked.connect(self.accept)
        self.footer.addWidget(ok)
        if cancel_text:
            cancel = self.make_button(cancel_text)
            cancel.clicked.connect(self.reject)
            self.footer.addWidget(cancel)
        self._ok_button = ok


def ask_confirm(parent, title, message, ok_text="确定", cancel_text="取消", danger=False):
    dialog = MessageDialog(parent, title, message, ok_text, cancel_text, danger)
    return dialog.exec_() == QDialog.Accepted


def notify(parent, title, message):
    MessageDialog(parent, title, message, "知道了").exec_()


class RuleDialog(CardDialog):
    """新增或编辑一条规则：触发器 + 动作"""

    def __init__(self, parent=None, rule=None, preset=None, category=None):
        data = copy.deepcopy(rule) if rule else (preset_rule(preset) or make_rule(
            "新规则", schedule_trigger(['09:00']),
            remind_action("提醒", "")))
        if category and not rule:
            data['category'] = category
        super().__init__(parent, "编辑规则" if rule else "新增规则",
                         "触发器决定什么时候提醒，动作决定到点做什么。", width=680)
        self.setWindowTitle("编辑规则" if rule else "新增提醒")
        self.setStyleSheet(dialog_qss() + _indicator_qss())
        self.setWindowIcon(create_icon())
        self.rule = None
        self._source = copy.deepcopy(rule) if rule else None
        trig = data.get('trigger') or {}
        action = data.get('action') or {}

        info = self.add_section("基本信息")
        self.name_edit = QLineEdit(data.get('name', ''))
        self.name_edit.setPlaceholderText("例如：上班打卡")
        info.addWidget(self.field_row("名称", self.name_edit))

        self.category_combo = QComboBox()
        for item in categories:
            self.category_combo.addItem(item['name'], item['id'])
        if self.category_combo.count() == 0:
            self.category_combo.addItem(CATEGORY_OTHER_NAME, CAT_OTHER)
        polish_combo(self.category_combo)
        self._set_combo(self.category_combo, data.get('category') or CAT_COMMUTE)
        info.addWidget(self.field_row("分类", self.category_combo))

        trigger_box = self.add_section("什么时候提醒")
        # 类别与时间分开：定时/开机 × 每日/工作日/自定义
        self.kind_combo = QComboBox()
        for value, label in TRIGGER_KIND_LABELS:
            self.kind_combo.addItem(label, value)
        polish_combo(self.kind_combo)
        self._set_combo(self.kind_combo, trigger_kind(trig))
        trigger_box.addWidget(self.field_row("触发类别", self.kind_combo))

        self.repeat_combo = QComboBox()
        for value, label in REPEAT_LABELS:
            self.repeat_combo.addItem(label, value)
        polish_combo(self.repeat_combo)
        self._set_combo(self.repeat_combo, trigger_repeat(trig))
        trigger_box.addWidget(self.field_row("时间", self.repeat_combo))

        self.times_edit = TimeField(_times_text({'trigger': trig}), multiple=True)
        self.times_edit.setPlaceholderText("08:00，多个用逗号隔开")
        self.row_times = self.field_row("提醒时间", self.times_edit)
        trigger_box.addWidget(self.row_times)

        day_row = QWidget()
        day_row.setAutoFillBackground(False)
        day_layout = QHBoxLayout(day_row)
        day_layout.setContentsMargins(0, 0, 0, 0)
        day_layout.setSpacing(10)
        self.day_boxes = []
        chosen = set(normalize_days(trig.get('weekdays')))
        if not chosen and trigger_repeat(trig) == REPEAT_CUSTOM:
            chosen = set(WORKDAYS)
        for idx, name in enumerate(WEEKDAY_CN):
            box = QCheckBox(name)
            box.setChecked(idx in chosen if chosen else True)
            self.day_boxes.append(box)
            day_layout.addWidget(box)
        day_layout.addStretch(1)
        self.row_days = self.field_row("生效星期", day_row)
        trigger_box.addWidget(self.row_days)

        window = trig.get('window') or {}
        self.window_from = TimeField(window.get('start', ''))
        self.window_from.setPlaceholderText("不限")
        self.window_from.setFixedWidth(96)
        self.window_to = TimeField(window.get('end', ''))
        self.window_to.setPlaceholderText("不限")
        self.window_to.setFixedWidth(96)
        window_row = QWidget()
        window_row.setAutoFillBackground(False)
        window_layout = QHBoxLayout(window_row)
        window_layout.setContentsMargins(0, 0, 0, 0)
        window_layout.setSpacing(8)
        window_layout.addWidget(self.window_from)
        arrow = QLabel("到")
        arrow.setObjectName("inlineHint")
        arrow.setAlignment(Qt.AlignCenter)
        arrow.setFixedWidth(18)
        window_layout.addWidget(arrow)
        window_layout.addWidget(self.window_to)
        window_layout.addStretch(1)
        # 说明单独占一行放在下面，别把窄输入框那一行挤变形
        window_block = QWidget()
        block_box = QVBoxLayout(window_block)
        block_box.setContentsMargins(0, 0, 0, 0)
        block_box.setSpacing(4)
        block_box.addWidget(window_row)
        note = QLabel("只在这段时间内触发，留空表示不限；可跨零点")
        note.setObjectName("hint")
        note.setContentsMargins(0, 0, 0, 0)
        block_box.addWidget(note)
        self.row_window = self.field_row("生效时段", window_block)
        trigger_box.addWidget(self.row_window)

        action_box = self.add_section("到点做什么")
        self.action_combo = QComboBox()
        for value, label in ACTION_LABELS:
            self.action_combo.addItem(label, value)
        polish_combo(self.action_combo)
        self._set_combo(self.action_combo, action.get('type', ACTION_REMIND))
        action_box.addWidget(self.field_row("动作", self.action_combo))

        self.title_edit = QLineEdit(action.get('title', ''))
        self.title_edit.setPlaceholderText("全屏提醒的大标题，例如：下班打卡")
        self.row_title = self.field_row("提示标题", self.title_edit)
        action_box.addWidget(self.row_title)

        self.content_edit = QPlainTextEdit(action.get('content', ''))
        self.content_edit.setFixedHeight(66)
        self.content_edit.setPlaceholderText("标题下面的内容，例如：请确认手机企微已打卡")
        self.row_content = self.field_row("提示内容", self.content_edit)
        action_box.addWidget(self.row_content)

        self.hint_edit = QLineEdit(action.get('hint', ''))
        self.hint_edit.setPlaceholderText("可选，小字补充说明")
        self.row_hint = self.field_row("补充说明", self.hint_edit)
        action_box.addWidget(self.row_hint)

        self.badge_edit = QLineEdit(action.get('badge', ''))
        self.badge_edit.setPlaceholderText("可选，顶部小标签，例如：上班提醒")
        self.row_badge = self.field_row("角标", self.badge_edit)
        action_box.addWidget(self.row_badge)

        self.button_edit = QLineEdit(action.get('button_text', ''))
        self.button_edit.setPlaceholderText("可选，按钮文案，默认「我知道了」")
        self.row_button = self.field_row("按钮文案", self.button_edit)
        action_box.addWidget(self.row_button)

        self.shutdown_seconds_spin = QSpinBox()
        self.shutdown_seconds_spin.setRange(5, 600)
        self.shutdown_seconds_spin.setSuffix(" 秒")
        self.shutdown_seconds_spin.setFixedWidth(104)
        self.shutdown_seconds_spin.setValue(
            int(action.get('auto_release_seconds') or DEFAULT_AUTO_RELEASE_SECONDS))
        self.row_shutdown_seconds = QWidget()
        seconds_row = QHBoxLayout(self.row_shutdown_seconds)
        seconds_row.setContentsMargins(0, 0, 0, 0)
        seconds_row.setSpacing(12)
        seconds_tag = QLabel("关机倒计时")
        seconds_tag.setObjectName("fieldLabel")
        seconds_tag.setFixedWidth(64)
        seconds_tag.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        seconds_row.addWidget(seconds_tag)
        seconds_row.addWidget(self.shutdown_seconds_spin)
        seconds_note = QLabel("到点关机时先弹提醒，这段时间内不操作就继续关机")
        seconds_note.setObjectName("inlineHint")
        seconds_note.setWordWrap(True)
        seconds_row.addWidget(seconds_note, 1)
        action_box.addWidget(self.row_shutdown_seconds)

        more = self.add_section("其他")
        self.enabled_check = QCheckBox("启用这条规则")
        self.enabled_check.setChecked(bool(data.get('enabled', True)))
        more.addWidget(self.enabled_check)
        self.snooze_check = QCheckBox("允许「稍后提醒」")
        self.snooze_check.setChecked(action.get('snooze_minutes') is not None)
        snooze_row = QHBoxLayout()
        snooze_row.setSpacing(10)
        snooze_row.addWidget(self.snooze_check)
        self.snooze_spin = QSpinBox()
        self.snooze_spin.setRange(1, 180)
        self.snooze_spin.setSuffix(" 分钟")
        self.snooze_spin.setFixedWidth(104)
        self.snooze_spin.setValue(int(action.get('snooze_minutes') or DEFAULT_SNOOZE_MINUTES))
        self.snooze_spin.setEnabled(self.snooze_check.isChecked())
        self.snooze_check.toggled.connect(self.snooze_spin.setEnabled)
        snooze_row.addWidget(self.snooze_spin)
        snooze_row.addStretch(1)
        self.row_snooze = QWidget()
        self.row_snooze.setLayout(snooze_row)
        more.addWidget(self.row_snooze)

        self.error_label = QLabel("")
        self.error_label.setObjectName("warnText")
        self.body.addWidget(self.error_label)

        cancel = self.make_button("取消")
        cancel.clicked.connect(self.reject)
        self.footer.addWidget(cancel)
        save = self.make_button("保存", primary=True)
        save.clicked.connect(self._save)
        self.footer.addWidget(save)

        self.kind_combo.currentIndexChanged.connect(self._sync_visibility)
        self.repeat_combo.currentIndexChanged.connect(self._sync_visibility)
        self.action_combo.currentIndexChanged.connect(self._sync_visibility)
        self._sync_visibility()

    @staticmethod
    def _set_combo(combo, value):
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _sync_visibility(self):
        kind = self.kind_combo.currentData()
        repeat = self.repeat_combo.currentData()
        # 只有「定时」才有具体时间点；「自定义」才要勾星期
        self.row_times.setVisible(kind == TRIGGER_SCHEDULE)
        self.row_days.setVisible(repeat == REPEAT_CUSTOM)
        reminder = self.action_combo.currentData() == ACTION_REMIND
        for widget in (self.row_title, self.row_content, self.row_hint,
                       self.row_badge, self.row_button):
            widget.setVisible(reminder)
        self.row_snooze.setVisible(reminder)
        # 只有「关机」动作要设倒计时（提醒窗口里自动关机前的等待时间）
        self.row_shutdown_seconds.setVisible(not reminder)

    def build_rule(self):
        """把界面上的值组装成规则字典，校验失败返回 (None, 错误说明)"""
        name = self.name_edit.text().strip()
        if not name:
            return None, "请填写规则名称"
        kind = self.kind_combo.currentData()
        repeat = self.repeat_combo.currentData()
        times_list = []
        if kind == TRIGGER_SCHEDULE:
            for chunk in re.split(r"[,，、\s]+", self.times_edit.text().strip()):
                if not chunk:
                    continue
                try:
                    hh, mm = parse_hhmm(chunk)
                except Exception:
                    return None, f"时间「{chunk}」格式不对，应为 HH:MM"
                if not (0 <= hh < 24 and 0 <= mm < 60):
                    return None, f"时间「{chunk}」超出范围"
                times_list.append(f"{hh:02d}:{mm:02d}")
            if not times_list:
                return None, "请至少填写一个提醒时间"
        days = []
        if repeat == REPEAT_CUSTOM:
            days = [i for i, box in enumerate(self.day_boxes) if box.isChecked()]
            if not days:
                return None, "请至少选择一个生效的星期"
        window = None
        start_text = self.window_from.text().strip()
        end_text = self.window_to.text().strip()
        if start_text or end_text:
            if not (start_text and end_text):
                return None, "生效时段要同时填写开始和结束时间"
            try:
                start = parse_hhmm(start_text)
                end = parse_hhmm(end_text)
            except Exception:
                return None, "生效时段格式不对，应为 HH:MM"
            if start == end:
                return None, "生效时段的开始和结束不能相同"
            window = {"start": "%02d:%02d" % start, "end": "%02d:%02d" % end}
        trigger = make_trigger(kind, repeat, days,
                              sorted(set(times_list)) if kind == TRIGGER_SCHEDULE else None,
                              window)
        action_type = self.action_combo.currentData()
        action = {'type': action_type}
        if action_type == ACTION_REMIND:
            action.update({
                'title': self.title_edit.text().strip() or name,
                'content': self.content_edit.toPlainText().strip(),
                'hint': self.hint_edit.text().strip(),
                'badge': self.badge_edit.text().strip() or '提醒',
                'button_text': self.button_edit.text().strip(),
                'snooze_minutes': (int(self.snooze_spin.value())
                                   if self.snooze_check.isChecked()
                                   else None),
            })
        if action_type == ACTION_SHUTDOWN:
            action['auto_release_seconds'] = int(self.shutdown_seconds_spin.value())
        old = self._source or {}
        return make_rule(name, trigger, action, self.enabled_check.isChecked(),
                         preset=old.get('preset'),
                         category=self.category_combo.currentData()), ''

    def _save(self):
        rule, error = self.build_rule()
        if rule is None:
            self.error_label.setText(error)
            return
        self.rule = rule
        self.accept()


def ask_rule(parent=None, rule=None, preset=None, category=None):
    """弹出规则对话框，返回新规则或 None"""
    dialog = RuleDialog(parent, rule, preset, category)
    if dialog.exec_() == QDialog.Accepted:
        return getattr(dialog, 'rule', None)
    return None

class CategoryDialog(CardDialog):
    """新建/重命名分类：只要一个名字输入框"""

    def __init__(self, parent=None, title="新建分类", name=""):
        super().__init__(parent, title, "分类只影响首页的分组显示，不影响提醒时间。", width=440)
        self.setStyleSheet(dialog_qss() + _indicator_qss())
        self.setWindowIcon(create_icon())

        self.name_edit = QLineEdit(name)
        self.name_edit.setPlaceholderText("例如：会议提醒")
        self.name_edit.selectAll()
        self.body.addWidget(self.field_row("名称", self.name_edit))

        self.error_label = QLabel("")
        self.error_label.setObjectName("warnText")
        self.body.addWidget(self.error_label)

        cancel = self.make_button("取消")
        cancel.clicked.connect(self.reject)
        self.footer.addWidget(cancel)
        save = self.make_button("保存", primary=True)
        save.clicked.connect(self._save)
        self.footer.addWidget(save)

        self.name_edit.returnPressed.connect(self._save)

    def _save(self):
        if not self.name_edit.text().strip():
            self.error_label.setText("分类名称不能为空")
            return
        self.accept()

    def value(self):
        return self.name_edit.text().strip()


def ask_category_name(parent, title, name=""):
    dialog = CategoryDialog(parent, title, name)
    if dialog.exec_() == QDialog.Accepted:
        return dialog.value()
    return None


class PreferencesDialog(CardDialog):
    """显示格式与延迟：从首页的「设置」按钮打开"""

    def __init__(self, parent=None):
        super().__init__(parent, "设置", "时间显示格式、主题外观、开机自启，以及卸载。", width=680)
        self.setWindowTitle(f"{APP_NAME} · 设置")
        self.setStyleSheet(dialog_qss() + _indicator_qss())
        self.setWindowIcon(create_icon())

        root = self.body

        display_panel = QFrame()
        display_panel.setObjectName("panel")
        display_box = QVBoxLayout(display_panel)
        display_box.setContentsMargins(14, 12, 14, 12)
        display_box.setSpacing(8)
        display_title = QLabel("时间显示格式")
        display_title.setObjectName("sectionTitle")
        display_box.addWidget(display_title)

        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("日期"))
        self.date_format_edit = QComboBox()
        fill_format_combo(self.date_format_edit, DATE_FORMAT_CHOICES,
                          options['display'].get('date_format', 'yyyy-MM-dd'))
        fmt_row.addWidget(self.date_format_edit)
        fmt_row.addSpacing(12)
        fmt_row.addWidget(QLabel("时间"))
        self.time_format_edit = QComboBox()
        fill_format_combo(self.time_format_edit, TIME_FORMAT_CHOICES,
                          options['display'].get('time_format', 'HH:mm:ss'))
        fmt_row.addWidget(self.time_format_edit)
        fmt_row.addStretch(1)
        display_box.addLayout(fmt_row)

        toggles = QHBoxLayout()
        self.weekday_check = QCheckBox("星期")
        self.lunar_check = QCheckBox("农历")
        self.ganzhi_check = QCheckBox("干支年")
        self.zodiac_check = QCheckBox("生肖")
        for combo in (self.date_format_edit, self.time_format_edit):
            polish_combo(combo)
            combo.currentIndexChanged.connect(self._apply_display)
        for box, key in ((self.weekday_check, 'show_weekday'), (self.lunar_check, 'show_lunar'),
                         (self.ganzhi_check, 'show_ganzhi'), (self.zodiac_check, 'show_zodiac')):
            box.setChecked(bool(options['display'].get(key, True)))
            box.toggled.connect(self._apply_display)
            toggles.addWidget(box)
        toggles.addStretch(1)
        display_box.addLayout(toggles)

        self.preview_label = QLabel("")
        self.preview_label.setObjectName("preview")
        display_box.addWidget(self.preview_label)

        fmt_hint = "格式串沿用 Qt：yyyy=四位年、yy=两位年、MM=月、dd=日、HH=24 小时制、mm=分、ss=秒。"
        self.date_format_edit.setToolTip(fmt_hint)
        self.time_format_edit.setToolTip(fmt_hint)
        root.addWidget(display_panel)

        look_panel = QFrame()
        look_panel.setObjectName("panel")
        look_box = QVBoxLayout(look_panel)
        look_box.setContentsMargins(14, 12, 14, 12)
        look_box.setSpacing(8)
        look_title = QLabel("外观")
        look_title.setObjectName("sectionTitle")
        look_box.addWidget(look_title)

        look_row = QHBoxLayout()
        look_row.addWidget(QLabel("主题"))
        self.theme_combo = QComboBox()
        for key, label in THEME_CHOICES:
            self.theme_combo.addItem(label, key)
        current_theme = (options.get('theme') or {}).get('name') or 'night'
        index = max(0, [c[0] for c in THEME_CHOICES].index(current_theme)
                    if current_theme in [c[0] for c in THEME_CHOICES] else 0)
        self.theme_combo.setCurrentIndex(index)
        self.theme_combo.setFixedWidth(120)
        polish_combo(self.theme_combo)
        self.theme_combo.currentIndexChanged.connect(self._apply_theme)
        look_row.addWidget(self.theme_combo)
        look_row.addSpacing(18)
        look_row.addWidget(QLabel("强调色"))
        self.accent_btn = QPushButton("选择颜色")
        self.accent_btn.setMinimumWidth(96)
        self.accent_btn.clicked.connect(self._pick_accent)
        look_row.addWidget(self.accent_btn)
        self.accent_reset_btn = QPushButton("跟随主题")
        self.accent_reset_btn.setObjectName("link")
        self.accent_reset_btn.clicked.connect(self._reset_accent)
        look_row.addWidget(self.accent_reset_btn)
        look_row.addStretch(1)
        look_box.addLayout(look_row)

        lang_row = QHBoxLayout()
        lang_row.addWidget(QLabel("语言"))
        self.language_combo = QComboBox()
        self.language_combo.addItem("中文", "zh")
        self.language_combo.addItem("English", "en")
        self.language_combo.setCurrentIndex(0 if language() == 'zh' else 1)
        self.language_combo.setFixedWidth(140)
        polish_combo(self.language_combo)
        self.language_combo.currentIndexChanged.connect(self._apply_language)
        lang_row.addWidget(self.language_combo)
        lang_row.addSpacing(18)
        lang_row.addWidget(QLabel("字号"))
        self.font_combo = QComboBox()
        for label, value in FONT_SCALE_CHOICES:
            self.font_combo.addItem(label, value)
        current_scale = font_scale()
        self.font_combo.setCurrentIndex(
            min(range(len(FONT_SCALE_CHOICES)),
                key=lambda i: abs(FONT_SCALE_CHOICES[i][1] - current_scale)))
        self.font_combo.setFixedWidth(130)
        polish_combo(self.font_combo)
        self.font_combo.currentIndexChanged.connect(self._apply_font_scale)
        lang_row.addWidget(self.font_combo)
        lang_row.addStretch(1)
        look_box.addLayout(lang_row)

        look_hint = QLabel("主题同时作用于提醒窗口、设置页和托盘菜单；强调色可单独指定，随时可恢复。")
        look_hint.setObjectName("hint")
        look_hint.setWordWrap(True)
        look_box.addWidget(look_hint)
        root.addWidget(look_panel)

        start_panel = QFrame()
        start_panel.setObjectName("panel")
        start_box = QVBoxLayout(start_panel)
        start_box.setContentsMargins(14, 12, 14, 12)
        start_box.setSpacing(8)
        start_title = QLabel("启动")
        start_title.setObjectName("sectionTitle")
        start_box.addWidget(start_title)
        check_row = QHBoxLayout()
        check_row.setSpacing(10)
        self.startup_check = QCheckBox(f"开机时自动启动 {APP_NAME}")
        self.startup_check.setChecked(is_startup_enabled())
        self.startup_check.toggled.connect(self._apply_startup)
        check_row.addWidget(self.startup_check)
        self.startup_chip = QLabel("未开启")
        self.startup_chip.setObjectName("stateChip")
        self.startup_chip.setAlignment(Qt.AlignCenter)
        check_row.addWidget(self.startup_chip, 0, Qt.AlignVCenter)
        check_row.addStretch(1)
        start_box.addLayout(check_row)
        self.startup_hint = QLabel("")
        self.startup_hint.setObjectName("startupHint")
        self.startup_hint.setWordWrap(True)
        start_box.addWidget(self.startup_hint)
        start_hint2 = QLabel("开机提醒、关机拦截都依赖程序在后台运行；关掉开机自启，重启后这些规则不会触发。")
        start_hint2.setObjectName("hint")
        start_hint2.setWordWrap(True)
        start_box.addWidget(start_hint2)
        root.addWidget(start_panel)

        danger_panel = QFrame()
        danger_panel.setObjectName("panel")
        danger_box = QVBoxLayout(danger_panel)
        danger_box.setContentsMargins(14, 12, 14, 12)
        danger_box.setSpacing(8)
        danger_title = QLabel("卸载")
        danger_title.setObjectName("sectionTitle")
        danger_box.addWidget(danger_title)
        danger_hint = QLabel("会删除全部提醒配置、开机自启、桌面快捷方式与注册表卸载信息，"
                             "然后删除程序目录；删除后不可恢复。")
        danger_hint.setObjectName("hint")
        danger_hint.setWordWrap(True)
        danger_box.addWidget(danger_hint)
        danger_box.addSpacing(4)
        danger_row = QHBoxLayout()
        danger_row.addStretch(1)
        uninstall_btn = QPushButton(f"卸载 {APP_NAME}")
        uninstall_btn.setObjectName("danger")
        uninstall_btn.clicked.connect(self._uninstall)
        danger_row.addWidget(uninstall_btn)
        danger_box.addLayout(danger_row)
        root.addWidget(danger_panel)

        self.status_label = QLabel("")
        self.status_label.setObjectName("hint")
        self.footer.insertWidget(0, self.status_label)
        about_btn = self.make_button("关于")
        about_btn.setToolTip("版本、作者与更新")
        about_btn.clicked.connect(lambda: open_about(self))
        self.footer.addWidget(about_btn)
        quit_btn = self.make_button("退出程序")
        quit_btn.setToolTip("关掉程序，之后定时提醒和关机拦截都不会再触发")
        quit_btn.clicked.connect(quit_program)
        self.footer.addWidget(quit_btn)
        close_btn = self.make_button("关闭", primary=True)
        close_btn.clicked.connect(self.accept)
        self.footer.addWidget(close_btn)

        self.date_format_edit.currentIndexChanged.connect(self._apply_display)
        self.time_format_edit.currentIndexChanged.connect(self._apply_display)
        self._refresh_preview()
        self._refresh_startup_hint()
        self._update_accent_button()

    def _apply_theme(self):
        current = options.get('theme') or {}
        options['theme'] = {
            'name': self.theme_combo.currentData() or 'night',
            'accent': current.get('accent') or '',
        }
        save_options()
        self.setStyleSheet(dialog_qss() + _indicator_qss())
        self._update_accent_button()
        apply_theme_everywhere()
        self._flash("外观已更新")

    def _pick_accent(self):
        current = (options.get('theme') or {}).get('accent') or theme()['accent']
        color = QColorDialog.getColor(QColor(current), self, "选择强调色")
        if not color.isValid():
            return
        options.setdefault('theme', {})
        options['theme']['accent'] = color.name()
        options['theme']['name'] = self.theme_combo.currentData() or 'night'
        save_options()
        self.setStyleSheet(dialog_qss() + _indicator_qss())
        self._update_accent_button()
        apply_theme_everywhere()
        self._flash("强调色已更新")

    def _reset_accent(self):
        options.setdefault('theme', {})
        if not options['theme'].get('accent'):
            self._flash("当前已是主题配色")
            return
        options['theme']['accent'] = ''
        options['theme']['name'] = self.theme_combo.currentData() or 'night'
        save_options()
        self.setStyleSheet(dialog_qss() + _indicator_qss())
        self._update_accent_button()
        apply_theme_everywhere()
        self._flash("已恢复主题配色")

    def _update_accent_button(self):
        accent = theme()['accent']
        self.accent_btn.setText("选择颜色")
        self.accent_btn.setToolTip("当前强调色 %s，点击更换" % accent)
        self.accent_btn.setStyleSheet(
            "QPushButton { background: %s; color: %s; border: none; font-weight: 600; }"
            "QPushButton:hover { background: %s; }" % (accent, theme()['on_accent'], theme()['accent_hover']))
        custom = bool((options.get('theme') or {}).get('accent'))
        self.accent_reset_btn.setText("跟随主题" if custom else "当前：主题配色")
        self.accent_reset_btn.setEnabled(custom)

    def _apply_startup(self, checked):
        """勾选后后台执行，期间禁用勾选框并显示等待光标"""
        self.startup_check.setEnabled(False)
        self.setCursor(Qt.WaitCursor)

        def done(ok, error):
            self.unsetCursor()
            self.startup_check.setEnabled(True)
            if not ok:
                notify(self, "设置失败",
                       f"无法修改开机自启：{error or '请检查启动文件夹权限。'}")
                self.startup_check.blockSignals(True)
                self.startup_check.setChecked(is_startup_enabled())
                self.startup_check.blockSignals(False)
            self._refresh_startup_hint()

        if set_startup_enabled_async(checked, done) is None:
            self.startup_check.setEnabled(True)
            self.unsetCursor()

    def _refresh_startup_hint(self):
        dependents = startup_dependent_rules()
        enabled = is_startup_enabled()
        tokens = theme()
        if enabled:
            chip_fg = tokens['success_text']
            text = ("开机后自动运行，开机提醒与关机拦截能正常触发。"
                    if dependents else "开机后自动运行。")
        else:
            chip_fg = tokens['countdown']
            if dependents:
                text = f"{len(dependents)} 条开机/关机规则需要程序开机自启才能触发。"
            else:
                text = "重启后程序不会运行，定时提醒也不会触发。"
        self.startup_chip.setText("已开启" if enabled else "未开启")
        self.startup_chip.setProperty("state", "on" if enabled else "off")
        self.startup_chip.style().unpolish(self.startup_chip)
        self.startup_chip.style().polish(self.startup_chip)
        self.startup_hint.setText(text)
        self.startup_hint.setProperty("warn", "false" if enabled else "true")
        self.startup_hint.style().unpolish(self.startup_hint)
        self.startup_hint.style().polish(self.startup_hint)

    def _uninstall(self):
        if not run_uninstall(self):
            return
        self.accept()
        if _settings_window is not None:
            _settings_window.close()
        if app is not None:
            app.quit()

    def _apply_font_scale(self):
        value = self.font_combo.currentData()
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 1.0
        self.options.setdefault('display', {})['font_scale'] = value
        save_options()
        apply_theme_everywhere()
        self.done(0)
        QTimer.singleShot(0, lambda: PreferencesDialog(self.parent()).exec_())

    def _apply_language(self):
        value = self.language_combo.currentData()
        if value == language():
            return
        options['language'] = value
        save_options()
        parent = self.parent()
        self.done(0)
        apply_language_everywhere()
        QTimer.singleShot(0, lambda: PreferencesDialog(parent).exec_())

    def _apply_display(self):
        options['display'].update({
            'date_format': self.date_format_edit.currentData() or 'yyyy-MM-dd',
            'time_format': self.time_format_edit.currentData() or 'HH:mm:ss',
            'show_weekday': self.weekday_check.isChecked(),
            'show_lunar': self.lunar_check.isChecked(),
            'show_ganzhi': self.ganzhi_check.isChecked(),
            'show_zodiac': self.zodiac_check.isChecked(),
        })
        save_options()
        self._refresh_preview()
        self._flash("已保存")

    def _refresh_preview(self):
        self.preview_label.setText(format_datetime(datetime.now()))

    def _flash(self, text):
        self.status_label.setText(text)
        self.status_label.setStyleSheet("color:%s;" % theme()['success_text'])
        QTimer.singleShot(1600, lambda: self.status_label.setStyleSheet("") or
                          self.status_label.setText(""))


RULE_MIME = "application/x-yisi-rule"


class RuleCard(QFrame):
    """规则卡片：按住卡片（非按钮处）拖动可调整顺序"""

    def __init__(self, rule, parent=None):
        super().__init__(parent)
        self.rule = rule
        self._press = None
        self.setObjectName("ruleCard")
        self.setCursor(Qt.OpenHandCursor)
        self.setToolTip("按住拖动可调整顺序")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press is None or not (event.buttons() & Qt.LeftButton):
            return
        if (event.pos() - self._press).manhattanLength() < QApplication.startDragDistance():
            return
        self._press = None
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(RULE_MIME, str(self.rule.get('id') or '').encode('utf-8'))
        drag.setMimeData(mime)
        pixmap = self.grab()
        drag.setPixmap(pixmap.scaled(max(1, pixmap.width() // 2),
                                     max(1, pixmap.height() // 2),
                                     Qt.KeepAspectRatio, Qt.SmoothTransformation))
        drag.setHotSpot(QPoint(pixmap.width() // 4, pixmap.height() // 4))
        drag.exec_(Qt.MoveAction)
        self.setCursor(Qt.OpenHandCursor)

    def mouseReleaseEvent(self, event):
        self._press = None
        self.setCursor(Qt.OpenHandCursor)
        super().mouseReleaseEvent(event)


class RuleDropArea(QWidget):
    """某个分类里的规则列表：接住拖来的卡片并按落点插入"""

    def __init__(self, cat_id, on_move, parent=None):
        super().__init__(parent)
        self.cat_id = cat_id
        self._on_move = on_move
        self.setAcceptDrops(True)
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(6)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(RULE_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(RULE_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if not event.mimeData().hasFormat(RULE_MIME):
            event.ignore()
            return
        rule_id = bytes(event.mimeData().data(RULE_MIME)).decode('utf-8')
        index = self._index_at(event.pos())
        event.acceptProposedAction()
        self._on_move(rule_id, self.cat_id, index)

    def _index_at(self, pos):
        """按落点的 y 落在哪张卡片的下半部分来算插入位置"""
        index = 0
        for i in range(self.box.count()):
            widget = self.box.itemAt(i).widget()
            if widget is None or not hasattr(widget, 'rule'):
                continue
            if pos.y() > widget.geometry().center().y():
                index += 1
        return index


class AboutDialog(CardDialog):
    """关于：版本、作者、仓库链接与自动更新"""

    def __init__(self, parent=None):
        super().__init__(parent, "关于", f"{APP_NAME} · 版本 {APP_VERSION}", width=600)
        self.setWindowTitle(f"{APP_NAME} · 关于")
        self.setStyleSheet(dialog_qss() + _indicator_qss())
        self.setWindowIcon(create_icon())
        self.release = None
        self._check_task = None
        self._download_task = None
        self._busy = False

        root = self.body

        info_box = self.add_section("版本信息")
        version_label = QLabel(APP_VERSION)
        version_label.setObjectName("versionValue")
        info_box.addWidget(self.field_row("版本", version_label))
        build_label = QLabel(build_time_text())
        info_box.addWidget(self.field_row("构建时间", build_label))
        location = installed_root() or app_dir()
        location_label = QLabel(location)
        location_label.setObjectName("hint")
        location_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        location_label.setWordWrap(True)
        info_box.addWidget(self.field_row("位置", location_label))

        author_box = self.add_section("作者")
        author_label = QLabel(f"{APP_AUTHOR}　微信 {APP_WECHAT}　QQ {APP_QQ}")
        author_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        author_box.addWidget(author_label)
        mail_label = QLabel(APP_MAIL)
        mail_label.setObjectName("hint")
        mail_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        author_box.addWidget(mail_label)

        link_box = self.add_section("开源与许可")
        link_row = QHBoxLayout()
        github_btn = self.make_button("打开 GitHub")
        github_btn.clicked.connect(lambda: open_in_browser(APP_HOME_URL))
        gitee_btn = self.make_button("打开 Gitee")
        gitee_btn.clicked.connect(lambda: open_in_browser(APP_MIRROR_URL))
        releases_btn = self.make_button("发布说明")
        releases_btn.clicked.connect(lambda: open_in_browser(APP_RELEASES_URL))
        for button in (github_btn, gitee_btn, releases_btn):
            link_row.addWidget(button)
        link_row.addStretch(1)
        link_box.addLayout(link_row)
        license_label = QLabel(f"许可证：{APP_LICENSE}（源码公开，禁止商用）")
        license_label.setObjectName("hint")
        license_label.setWordWrap(True)
        link_box.addWidget(license_label)

        update_box = self.add_section("更新")
        self.status_label = QLabel("检查更新时会连接 GitHub 与 Gitee。")
        self.status_label.setObjectName("hint")
        self.status_label.setWordWrap(True)
        update_box.addWidget(self.status_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setFixedHeight(8)
        self.progress.setTextVisible(False)
        self.progress.hide()
        update_box.addWidget(self.progress)
        update_row = QHBoxLayout()
        update_row.addStretch(1)
        self.check_btn = self.make_button("检查更新")
        self.check_btn.clicked.connect(self.check_update)
        update_row.addWidget(self.check_btn)
        self.install_btn = self.make_button("下载并安装", primary=True)
        self.install_btn.clicked.connect(self.download_and_install)
        self.install_btn.hide()
        update_row.addWidget(self.install_btn)
        update_box.addLayout(update_row)

        self.status_label_footer = QLabel("")
        self.status_label_footer.setObjectName("hint")
        self.footer.insertWidget(0, self.status_label_footer)
        close_btn = self.make_button("关闭", primary=True)
        close_btn.clicked.connect(self.accept)
        self.footer.addWidget(close_btn)

    # ---------- 交互 ----------
    def _set_busy(self, busy, text=""):
        self._busy = busy
        self.check_btn.setEnabled(not busy)
        if text is not None:
            self.status_label.setText(text)

    def _set_status(self, text, warn=False):
        self.status_label.setText(text)
        self.status_label.setObjectName("warnText" if warn else "hint")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def check_update(self):
        if self._busy:
            return
        self.install_btn.hide()
        self.progress.hide()
        self._set_busy(True, "正在检查更新…")
        self._check_task = UpdateCheckTask(self)
        self._check_task.done.connect(self._on_checked)
        self._check_task.failed.connect(self._on_check_failed)
        self._check_task.start()

    def _on_checked(self, release):
        self._set_busy(False)
        self.release = release
        if newer_version(release.get("version")):
            self._set_status(f"发现新版本 {release['version']}（当前 {APP_VERSION}），"
                             f"可直接下载安装，会自动关闭程序并重开。")
            self.install_btn.show()
        else:
            self._set_status(f"已是最新版本（{APP_VERSION}）。")

    def _on_check_failed(self, message):
        self._set_busy(False)
        self._set_status(message or "检查更新失败，请检查网络。", warn=True)

    def download_and_install(self):
        if self._busy or not self.release:
            return
        folder = os.path.join(tempfile.gettempdir(), f"yisi-timer-update-{self.release['version']}")
        os.makedirs(folder, exist_ok=True)
        self._set_busy(True, "正在下载安装包…")
        self.install_btn.hide()
        self.progress.setValue(0)
        self.progress.show()
        self._download_task = UpdateDownloadTask(self.release, folder, self)
        self._download_task.progress.connect(self._on_progress)
        self._download_task.done.connect(self._on_downloaded)
        self._download_task.failed.connect(self._on_download_failed)
        self._download_task.start()

    def _on_progress(self, done, total):
        if total > 0:
            self.progress.setValue(min(100, int(done * 100 / total)))
            self.status_label.setText(
                f"正在下载安装包… {done / 1048576:.1f} / {total / 1048576:.1f} MB")
        else:
            self.status_label.setText(f"正在下载安装包… {done / 1048576:.1f} MB")

    def _on_downloaded(self, installer_path, manifest_path):
        self.progress.setValue(100)
        self.status_label.setText("签名与校验值核对通过，正在安装…")
        ok, error = launch_update(installer_path)
        self._set_busy(False)
        if not ok:
            notify(self, "无法自动安装", error)
            open_in_browser(APP_RELEASES_URL)
            return
        _log(f"开始更新：{self.release.get('version')}（{installer_path}）")
        self.accept()
        if _settings_window is not None:
            _settings_window.close()
        quit_program()

    def _on_download_failed(self, message):
        self._set_busy(False)
        self.progress.hide()
        self.install_btn.show()
        self._set_status(message or "下载失败，请稍后重试。", warn=True)


_about_window = None


def open_about(parent=None):
    """托盘菜单与设置页共用同一个关于窗口"""
    global _about_window
    if _about_window is None or not _about_window.isVisible():
        _about_window = AboutDialog(parent)
    _about_window.show()
    _about_window.raise_()
    _about_window.activateWindow()
    return _about_window


class HomeWindow(CardDialog):
    """首页：按分类列出所有提醒，分类与提醒的增删改都在这里"""

    def __init__(self):
        super().__init__(None, APP_NAME,
                         "按分类管理提醒：新增、编辑、启用/停用，按住卡片可拖动排序，点「预览」看实际效果。",
                         width=960, auto_fit=False, use_scroll=False, modal=False)
        self.setWindowTitle(APP_NAME)
        self.resize(940, 700)
        self.setStyleSheet(dialog_qss() + _indicator_qss())
        self.setWindowIcon(create_icon())

        root = self.body

        # 首页计时区域：时间格式、星期、农历、干支、生肖都跟「设置 → 时间显示格式」走
        self.clock_label = QLabel("")
        self.clock_label.setObjectName("clockBar")
        self.clock_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.clock_label)
        self.clock_timer = QTimer(self)
        self.clock_timer.setInterval(1000)
        self.clock_timer.timeout.connect(self._tick_clock)
        self.clock_timer.start()
        self._tick_clock()

        new_cat_btn = QPushButton("＋ 新建分类")
        new_cat_btn.clicked.connect(self.new_category)
        self.header_extra.addWidget(new_cat_btn)

        prefs_btn = QPushButton("设置")
        prefs_btn.clicked.connect(self.open_preferences)
        self.header_extra.addWidget(prefs_btn)

        self.banner_box = QVBoxLayout()
        self.banner_box.setContentsMargins(0, 0, 0, 0)
        root.addLayout(self.banner_box)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.viewport().setAutoFillBackground(False)
        container = QWidget()
        container.setAutoFillBackground(False)
        self.list_box = QVBoxLayout(container)
        self.list_box.setContentsMargins(2, 2, 8, 2)
        self.list_box.setSpacing(14)
        self.list_box.addStretch(1)
        self.scroll.setWidget(container)
        root.addWidget(self.scroll, 1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("hint")
        self.footer.insertWidget(0, self.status_label)
        close_btn = self.make_button("关闭")
        close_btn.clicked.connect(self.close)
        self.footer.addWidget(close_btn)

        self.reload()

    # ---------- 计时 ----------
    def _tick_clock(self):
        self.clock_label.setText(t("现在：") + format_datetime(datetime.now()))

    # ---------- 刷新 ----------
    def reload(self):
        """整页重建：规则数量不大，重建比增量刷新更不容易出错"""
        while self.list_box.count() > 1:
            item = self.list_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # 先摘下来再延迟销毁，避免重建瞬间新旧面板叠在一起
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()

        grouped = {item['id']: [] for item in categories}
        uncategorized = []
        for rule in rules:
            cid = category_of_rule(rule)
            if cid == CAT_OTHER:
                uncategorized.append(rule)
            else:
                grouped.setdefault(cid, []).append(rule)

        index = 0
        for cat in categories:
            panel = self._category_panel(cat['id'], cat['name'], grouped.get(cat['id'], []), True)
            self.list_box.insertWidget(index, panel)
            index += 1
        if uncategorized:
            panel = self._category_panel(CAT_OTHER, CATEGORY_OTHER_NAME, uncategorized, True)
            self.list_box.insertWidget(index, panel)
            index += 1
        if not categories and not uncategorized:
            self.list_box.insertWidget(0, self._empty_panel())

        self.status_label.setText(f"{len(rules)} 条提醒 · {len(categories)} 个分类")
        translate = globals().get('translate_widgets')
        if translate is not None:
            translate(self)
        self._refresh_banner()
        self.scroll.verticalScrollBar().setValue(0)

    def _refresh_banner(self):
        """开机自启提示条：开机提醒/关机拦截依赖程序开机自动运行"""
        while self.banner_box.count():
            item = self.banner_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        dependents = startup_dependent_rules()
        if not dependents:
            return

        bar = QFrame()
        row = QHBoxLayout(bar)
        row.setContentsMargins(14, 10, 12, 10)
        row.setSpacing(10)
        text = QLabel()
        text.setWordWrap(True)
        row.addWidget(text, 1)

        if is_startup_enabled():
            bar.setObjectName("tipBar")
            text.setObjectName("tipText")
            text.setText(t("开机自启已开启 · {0} 条开机/关机规则会在开机后自动生效")
                         .format(len(dependents)))
        else:
            bar.setObjectName("warnBar")
            text.setObjectName("warnText")
            text.setText(
                t("开机自启未开启 · {0} 条开机/关机规则不会生效："
                  "重启后程序不会自动运行，开机提醒与关机拦截都收不到。").format(len(dependents)))
            enable_btn = QPushButton("立即开启开机自启")
            enable_btn.setObjectName("primary")
            enable_btn.clicked.connect(lambda _checked=False, b=enable_btn: self.enable_autostart(b))
            row.addWidget(enable_btn)
        self.banner_box.addWidget(bar)

    def enable_autostart(self, button=None):
        """后台创建启动快捷方式；期间按钮显示加载态并禁止重复点击"""
        if button is not None:
            button.setEnabled(False)
            button.setText("正在开启…")
        self.setCursor(Qt.WaitCursor)

        def done(ok, error):
            self.unsetCursor()
            if ok:
                self._flash("已开启开机自启")
            else:
                notify(self, "设置失败",
                       f"开启开机自启失败：{error or '请检查启动文件夹权限。'}")
            self.reload()

        if set_startup_enabled_async(True, done) is None and button is not None:
            button.setText("正在开启…")

    def _empty_panel(self):
        """全新安装：没有分类也没有提醒时的引导面板"""
        panel = QFrame()
        panel.setObjectName("panel")
        box = QVBoxLayout(panel)
        box.setContentsMargins(20, 22, 20, 22)
        box.setSpacing(10)

        title = QLabel("还没有分类")
        title.setObjectName("sectionTitle")
        box.addWidget(title)

        hint = QLabel("提醒按分类摆放：先建一个分类（例如「上下班提醒」「定时关机」），"
                      "再在分类里点「＋ 新增」添加提醒。分类和提醒都可以随时改名或删除。")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        box.addWidget(hint)

        row = QHBoxLayout()
        row.setContentsMargins(0, 4, 0, 0)
        btn = self.make_button("＋ 新建分类", primary=True)
        btn.clicked.connect(self.new_category)
        row.addWidget(btn)
        row.addStretch(1)
        box.addLayout(row)
        return panel

    def _category_panel(self, cat_id, name, cat_rules, editable):
        panel = QFrame()
        panel.setObjectName("panel")
        box = QVBoxLayout(panel)
        box.setContentsMargins(16, 12, 16, 14)
        box.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel(name)
        title.setObjectName("sectionTitle")
        header.addWidget(title)
        count = QLabel(f"{len(cat_rules)} 条")
        count.setObjectName("catCount")
        header.addWidget(count)
        header.addStretch(1)

        add_btn = QToolButton()
        add_btn.setText("＋ 新增 ▾")
        add_btn.setPopupMode(QToolButton.InstantPopup)
        _t = theme()
        add_btn.setStyleSheet(
            "QToolButton{background-color:%s;"
            "border:1px solid %s;border-radius:8px;padding:5px 12px;color:%s;}"
            "QToolButton:hover{background-color:%s;border-color:%s;}"
            "QToolButton::menu-indicator{image:none;}"
            % (_t['accent_fill'], _t['accent_border'], _t['countdown'],
               _t['accent_border'], _t['accent_border_strong']))
        add_menu = RoundMenu(add_btn)
        add_menu.setStyleSheet(menu_qss())
        for preset in PRESETS:
            add_menu.addAction(preset['label'],
                               lambda checked=False, k=preset['key'], c=cat_id:
                               self.add_rule(c, preset=k))
        add_menu.addSeparator()
        add_menu.addAction("自定义提醒", lambda checked=False, c=cat_id: self.add_rule(c))
        add_btn.setMenu(add_menu)
        header.addWidget(add_btn)

        if editable and cat_id != CAT_OTHER:
            rename_btn = QPushButton("重命名")
            rename_btn.setObjectName("link")
            rename_btn.clicked.connect(lambda checked=False, c=cat_id, n=name:
                                       self.rename_category(c, n))
            header.addWidget(rename_btn)
            del_btn = QPushButton("删除分类")
            del_btn.setObjectName("link")
            del_btn.clicked.connect(lambda checked=False, c=cat_id, n=name:
                                    self.delete_category(c, n))
            header.addWidget(del_btn)
        box.addLayout(header)

        holder = RuleDropArea(cat_id, self.move_rule)
        if not cat_rules:
            empty = QLabel("这个分类下还没有提醒，点「＋ 新增」添加一条。")
            empty.setObjectName("hint")
            holder.box.addWidget(empty)
        for rule in cat_rules:
            holder.box.addWidget(self._rule_card(rule))
        box.addWidget(holder)
        return panel

    def _rule_card(self, rule):
        card = RuleCard(rule)
        row = QHBoxLayout(card)
        row.setContentsMargins(12, 8, 10, 8)
        row.setSpacing(10)

        check = QCheckBox()
        check.setChecked(bool(rule.get('enabled', True)))
        check.setToolTip("启用/停用这条提醒")
        check.toggled.connect(lambda checked, r=rule: self.toggle_rule(r, checked))
        row.addWidget(check)

        text_box = QVBoxLayout()
        text_box.setSpacing(2)
        name = QLabel(rule.get('name') or '提醒')
        name.setStyleSheet("color:%s;font-size:14px;font-weight:600;" % theme()['text_strong'])
        text_box.addWidget(name)
        meta = QLabel(f"{describe_trigger(rule)} · {describe_action(rule)}")
        meta.setObjectName("hint")
        text_box.addWidget(meta)
        row.addLayout(text_box)
        row.addStretch(1)

        preview_btn = QPushButton("预览")
        preview_btn.setObjectName("link")
        preview_btn.clicked.connect(lambda checked=False, r=rule: preview_rule(r))
        row.addWidget(preview_btn)

        edit_btn = QPushButton("编辑")
        edit_btn.setObjectName("link")
        edit_btn.clicked.connect(lambda checked=False, r=rule: self.edit_rule(r))
        row.addWidget(edit_btn)

        del_btn = QPushButton("删除")
        del_btn.setObjectName("link")
        del_btn.clicked.connect(lambda checked=False, r=rule: self.delete_rule(r))
        row.addWidget(del_btn)
        return card

    # ---------- 规则 ----------
    def move_rule(self, rule_id, cat_id, index):
        """把规则拖到某个分类的第 index 个位置（同分类内即调整顺序）"""
        moved = next((r for r in rules if str(r.get('id')) == rule_id), None)
        if moved is None:
            return
        ids = [str(r.get('id')) for r in rules if category_of_rule(r) == cat_id]
        positions = [i for i, r in enumerate(rules) if category_of_rule(r) == cat_id]
        if rule_id in ids:
            old = ids.index(rule_id)
            ids.pop(old)
            if old < index:
                index -= 1
        ids.insert(max(0, min(index, len(ids))), rule_id)

        lookup = {str(r.get('id')): r for r in rules if category_of_rule(r) == cat_id}
        lookup[rule_id] = moved
        moved['category'] = cat_id
        seq = [lookup[rid] for rid in ids]

        anchor = min(positions) if positions else len(rules)
        rest = [r for r in rules if category_of_rule(r) != cat_id]
        rules[:] = rest[:anchor] + seq + rest[anchor:]
        save_rules()
        schedule_rules()
        update_menu()
        self.reload()
        self._flash("已调整顺序")

    def add_rule(self, cat_id=None, preset=None):
        rule = ask_rule(self, preset=preset, category=cat_id)
        if rule is None:
            return
        rules.append(rule)
        save_rules()
        schedule_rules()
        update_menu()
        self.reload()
        self._flash("已添加提醒")

    def edit_rule(self, rule):
        updated = ask_rule(self, rule=rule)
        if updated is None:
            return
        index = rules.index(rule)
        rules[index] = updated
        save_rules()
        schedule_rules()
        update_menu()
        self.reload()
        self._flash("已保存")

    def delete_rule(self, rule):
        if not ask_confirm(self, "删除提醒", f"确定删除「{rule.get('name', '')}」？",
                           ok_text="删除", danger=True):
            return
        rules.remove(rule)
        save_rules()
        schedule_rules()
        update_menu()
        self.reload()
        self._flash("已删除提醒")

    def toggle_rule(self, rule, checked):
        rule['enabled'] = bool(checked)
        save_rules()
        schedule_rules()
        update_menu()
        self._flash("已启用" if checked else "已停用")

    # ---------- 分类 ----------
    def new_category(self):
        name = ask_category_name(self, "新建分类")
        if name is None:
            return
        if any(item['name'] == name for item in categories):
            notify(self, "分类重复", f"已经有叫「{name}」的分类了。")
            return
        cid = f"cat{int(time.time() * 1000):x}"
        categories.append({"id": cid, "name": name})
        save_categories()
        self.reload()
        self._flash("已新建分类")

    def rename_category(self, cat_id, old_name):
        name = ask_category_name(self, "重命名分类", old_name)
        if name is None or name == old_name:
            return
        if any(item['name'] == name and item['id'] != cat_id for item in categories):
            notify(self, "分类重复", f"已经有叫「{name}」的分类了。")
            return
        for item in categories:
            if item['id'] == cat_id:
                item['name'] = name
                break
        save_categories()
        self.reload()
        self._flash("已重命名")

    def delete_category(self, cat_id, name):
        moved = [r for r in rules if category_of_rule(r) == cat_id]
        tip = f"确定删除分类「{name}」？"
        if moved:
            tip += f"\n里面的 {len(moved)} 条提醒会挪到「{CATEGORY_OTHER_NAME}」，不会被删除。"
        if not ask_confirm(self, "删除分类", tip, ok_text="删除", danger=True):
            return
        categories[:] = [item for item in categories if item['id'] != cat_id]
        for rule in moved:
            rule['category'] = CAT_OTHER
        save_categories()
        save_rules()
        self.reload()
        self._flash("已删除分类")

    # ---------- 其他 ----------
    def open_preferences(self):
        PreferencesDialog(self).exec_()
        self.reload()

    def closeEvent(self, event):
        """点 ✕ 或按 Esc 只收进托盘，真正退出走设置或托盘菜单里的「退出」"""
        if _exiting:
            event.accept()
            return
        event.ignore()
        self.hide()

    def _flash(self, text):
        self.status_label.setText(text)
        self.status_label.setStyleSheet("color:%s;" % theme()['success_text'])
        QTimer.singleShot(1800, lambda: self.status_label.setStyleSheet("") or
                          self.status_label.setText(
                              f"{len(rules)} 条提醒 · {len(categories)} 个分类"))

# -------------------- 托盘菜单 --------------------
# 置 True 后才允许窗口真正关闭（退出程序时）
_exiting = False


def quit_program():
    """退出整个程序（设置页与托盘菜单共用）"""
    global _exiting
    _exiting = True
    # 先把打开着的对话框都收掉，否则模态框的 exec_ 循环会挡着退出
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, QDialog) and widget.isVisible():
            try:
                widget.done(0)
            except Exception:
                widget.close()
    app.quit()


def on_tray_activated(reason):
    """单击托盘图标打开首页（右键仍然是菜单）"""
    if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
        open_settings()


def open_settings():
    global _settings_window
    if _settings_window is None:
        _settings_window = HomeWindow()
    _settings_window.reload()
    _settings_window.show()
    _settings_window.raise_()
    _settings_window.activateWindow()

def toggle_pause():
    """暂停/恢复所有定时提醒（关机拦截与手动预览不受影响）"""
    global paused_until
    if paused_until and datetime.now() < paused_until:
        paused_until = None
        _log("恢复定时提醒")
    else:
        paused_until = datetime.now() + timedelta(hours=1)
        _log("暂停定时提醒 1 小时")
    update_menu()

def update_menu():
    menu = globals().get('menu')  # 测试或未建托盘时直接跳过
    if menu is None:
        return
    menu.clear()
    settings_action = menu.addAction("打开设置")
    settings_action.triggered.connect(open_settings)
    menu.addSeparator()
    paused = bool(paused_until and datetime.now() < paused_until)
    pause_action = menu.addAction("恢复提醒" if paused else "暂停提醒（1 小时）")
    pause_action.triggered.connect(toggle_pause)
    summary = "已暂停" if paused else (next_rule_summary() or "暂无定时规则")
    next_action = menu.addAction(f"下一条：{summary}")
    next_action.setEnabled(False)
    menu.addSeparator()
    startup_action = menu.addAction("✓ 开机自启" if is_startup_enabled() else "开机自启")
    startup_action.triggered.connect(toggle_startup)
    about_action = menu.addAction("关于")
    about_action.triggered.connect(lambda: open_about())
    menu.addSeparator()
    quit_action = menu.addAction("退出")
    quit_action.triggered.connect(quit_program)
    translate_menu(menu)

# -------------------- 主程序 --------------------
class PointerCursorFilter(QObject):
    """可点击控件统一显示手型光标：Qt 样式表设不了 cursor，只能在控件上逐个设"""

    CLICKABLE = (QPushButton, QToolButton, QCheckBox, QRadioButton, QComboBox, QMenu)
    VIEWS = (QListWidget, QTableWidget, QAbstractItemView)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Polish:
            try:
                self._apply(obj)
            except Exception:
                pass
        return False

    def _apply(self, widget):
        if isinstance(widget, self.CLICKABLE):
            widget.setCursor(Qt.PointingHandCursor)
        if isinstance(widget, self.VIEWS):
            widget.setCursor(Qt.PointingHandCursor)
            viewport = widget.viewport() if hasattr(widget, 'viewport') else None
            if viewport is not None:
                viewport.setCursor(Qt.PointingHandCursor)


def install_pointer_cursor(application):
    """装上事件过滤器；返回过滤器对象避免被回收"""
    filter_obj = PointerCursorFilter(application)
    application.installEventFilter(filter_obj)
    application._pointer_filter = filter_obj
    return filter_obj


def main():
    global app, tray, menu
    # 高分屏下按缩放比例布局，否则全屏窗口会比实际屏幕大、只显示一部分
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    # 125%/150% 这类分数缩放：默认四舍五入会让字变小再被放大（发虚），
    # PassThrough 让 Qt 按真实比例渲染
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception:
        pass
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    install_pointer_cursor(app)

    if '--uninstall' in sys.argv:
        run_uninstall(assume_yes='--yes' in sys.argv)
        sys.exit(0)

    load_options()
    load_categories()
    load_rules()
    save_rules()   # 老规则补上分类字段后落盘
    register_uninstall_info()

    heartbeat = QTimer()
    heartbeat.setInterval(300000)
    heartbeat.timeout.connect(lambda: _log("心跳：程序仍在运行"))
    heartbeat.start()
    _log("启动：规则 %s 条，自启=%s" % (
        len([r for r in rules if r.get('enabled', True)]), is_startup_enabled()))

    tray = QSystemTrayIcon()
    tray.setIcon(create_icon())
    tray.setToolTip(APP_NAME)
    menu = RoundMenu()
    menu.setStyleSheet(menu_qss())
    update_menu()
    tray.setContextMenu(menu)
    tray.activated.connect(on_tray_activated)
    tray.show()

    schedule_rules()
    # 启动后先把界面打开一次，方便直接看到今天的提醒
    QTimer.singleShot(400, open_settings)
    # 再延迟一点弹出开机提醒：它盖在界面上，关掉后还能看到首页
    QTimer.singleShot(1400, show_boot_reminders)
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
