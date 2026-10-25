"""Local first-run account and budget setup; never send a network request."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_limits(values):
    from server.usage import COUNTER_KEYS, MAX_INTEGER
    result = {}
    for name in COUNTER_KEYS:
        raw = values[name].strip()
        if raw == '不限':
            result[name] = None
        elif raw.isascii() and raw.isdecimal() and 0 < int(raw) <= MAX_INTEGER:
            result[name] = int(raw)
        else:
            raise ValueError('请填写正整数或“不限”。三项都需要填写。')
    return result


def current_state(root):
    from server import usage
    from tools.start_plan import read_credential, StartupError
    try:
        from server.provider_routes import RuntimeRoutes
        if RuntimeRoutes.load(root) is None:
            read_credential(root / 'config/provider-key.txt')
        key_ready = True
    except (StartupError, ValueError, OSError):
        key_ready = False
    try:
        limits = usage._limits()
        ready = all(value is None or value > 0 for value in limits.values())
    except usage.UsageError:
        limits, ready = {}, False
    return key_ready, limits, ready


def startup_mode(root):
    path = root / 'config/startup-mode.json'
    try:
        mode = json.loads(path.read_text(encoding='utf-8')).get('mode')
    except (OSError, ValueError, AttributeError):
        return None
    return mode if mode in ('cloud', 'simulation') else None


def save_startup_mode(root, mode):
    if mode not in ('cloud', 'simulation'):
        raise ValueError('Invalid startup mode')
    from tools.portable_runtime import plain_path
    target = plain_path(root, 'config/startup-mode.json')
    temporary = target.with_name('.startup-mode-' + secrets.token_hex(8) + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as output:
            output.write(json.dumps({'mode': mode}, ensure_ascii=False, indent=2) + chr(10))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def save_settings(root, candidate, values, *, keep_key):
    # Validate every field before changing either configuration file.
    from server.provider_config import ProviderSettings, validate_key
    from tools.portable_runtime import plain_path
    limits = parse_limits(values)
    if not keep_key:
        validate_key(candidate)
    target = plain_path(root, 'config/usage-limits.json')
    temporary = target.with_name('.limits-' + secrets.token_hex(8) + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as output:
            output.write(json.dumps(limits, ensure_ascii=False, indent=2) + chr(10))
            output.flush()
            os.fsync(output.fileno())
        if not keep_key:
            settings = ProviderSettings(root, None)
            settings.save(dict(revision=settings.revision(), key=candidate))
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def setup_window(root):
    import tkinter as tk
    from tkinter import ttk, messagebox
    from server.usage import COUNTER_KEYS
    key_ready, limits, _ = current_state(root)
    window = tk.Tk()
    window.title('知夏 · 首次配置 / API 与用量')
    window.geometry('680x650')
    window.minsize(640, 630)
    window.configure(bg='#f3f5ef')
    style = ttk.Style(window)
    style.theme_use('clam')
    style.configure('TFrame', background='#f3f5ef')
    style.configure('TLabel', background='#f3f5ef', foreground='#263b34', font=('Microsoft YaHei UI', 10))
    style.configure('TButton', font=('Microsoft YaHei UI', 11), padding=9)
    frame = ttk.Frame(window, padding=28)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='让知夏在这台电脑上与你相伴', font=('Microsoft YaHei UI', 18, 'bold')).pack(anchor='w', pady=(0, 12))
    ttk.Label(frame, text='此处用于保存百炼账号、每日上限和启动模式。其他平台可先离线启动，在“人物与声音 → 04 API 配置”中分别配置文字、记忆、合成与识别，再从开始菜单运行“配置知夏”设置每日上限。', wraplength=600).pack(anchor='w')
    ttk.Label(frame, text='API Key（本机 Windows 用户加密保存）').pack(anchor='w', pady=(18, 6))
    key = tk.StringVar()
    entry = ttk.Entry(frame, textvariable=key, show='●', font=('Microsoft YaHei UI', 11))
    entry.pack(fill='x')
    ttk.Label(frame, text='已有可读取账号；留空保留，填写则更换。' if key_ready else '填写 sk- 开头的 Token Plan API Key。换电脑后需要重新填写。').pack(anchor='w', pady=6)
    ttk.Label(frame, text='每日使用上限（北京时间按日累计）', font=('Microsoft YaHei UI', 12, 'bold')).pack(anchor='w', pady=(14, 8))
    fields = {}
    grid = ttk.Frame(frame)
    grid.pack(fill='x')
    for index, (name, label, default) in enumerate(zip(COUNTER_KEYS, ('文字请求次数', '语音合成次数', '估计语音字符'), (100, 30, 5000))):
        ttk.Label(grid, text=label).grid(row=index, column=0, sticky='w', pady=5)
        value = limits.get(name, 0)
        fields[name] = tk.StringVar(value='不限' if value is None else str(value or default))
        ttk.Entry(grid, textvariable=fields[name], width=22).grid(row=index, column=1, sticky='w', padx=24, pady=5)
    ttk.Label(frame, text='可填写正整数或“不限”。后台记忆、情绪请求也计入文字次数；失败或取消也计数。这是本地限制，不代表账号余额或账单；语音识别不受这三项上限控制。', wraplength=600).pack(anchor='w', pady=12)
    agreed = tk.BooleanVar(value=False)
    ttk.Checkbutton(frame, text='我同意启用云服务，并承担账号实际产生的用量', variable=agreed).pack(anchor='w', pady=6)
    ttk.Label(frame, text='保存只检查本地格式和加密；不发送测试请求。启动正式服务后，记忆和对话功能可能产生账号用量。未配置也可使用“离线体验”。', wraplength=600).pack(anchor='w', pady=10)
    result = 'cancel'
    def apply():
        nonlocal result
        if not agreed.get():
            messagebox.showinfo('尚未启用', '请先勾选同意启用云服务，或选择“先离线体验”。', parent=window)
            return
        candidate = key.get().strip()
        from server.provider_routes import RouteStore
        if candidate and RouteStore(root).path.exists():
            messagebox.showinfo('请使用连接管理', '已启用分任务 API 配置。请留空保留账号；更换密钥请到 04 API 配置中修改对应连接。', parent=window)
            return
        try:
            save_settings(root, candidate, {name: value.get() for name, value in fields.items()}, keep_key=key_ready and not candidate)
            save_startup_mode(root, 'cloud')
        except ValueError:
            messagebox.showerror('请检查填写内容', '密钥应为 sk- 开头且不含空格。每日上限需为正整数或“不限”。', parent=window)
            return
        except Exception:
            messagebox.showerror('保存未完成', '无法写入本机配置，请检查目录权限后重新保存；本次未启动云服务。', parent=window)
            return
        key.set('')
        result = 'cloud'
        window.destroy()
    def offline():
        nonlocal result
        try:
            save_startup_mode(root, 'simulation')
        except Exception:
            messagebox.showerror('保存未完成', '无法保存离线启动偏好，请检查目录权限后重试。', parent=window)
            return
        result = 'simulation'
        window.destroy()
    buttons = ttk.Frame(frame)
    buttons.pack(fill='x', pady=8)
    ttk.Button(buttons, text='保存并启动', command=apply).pack(side='right')
    ttk.Button(buttons, text='先离线体验', command=offline).pack(side='right', padx=10)
    entry.focus_set()
    window.mainloop()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--configure', action='store_true')
    parser.add_argument('--simulation', action='store_true')
    parser.add_argument('--acceptance', action='store_true')
    parser.add_argument('--development', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    portable = (ROOT / 'portable.json').is_file()
    if not portable and not (args.development and (ROOT / 'tools/build_portable.py').is_file()):
        print('This entry is for the complete Windows distribution only.')
        return 1
    if portable:
        from tools.portable_runtime import preflight
        preflight(ROOT)
    if not args.simulation and not args.configure and startup_mode(ROOT) == 'simulation':
        args.simulation = True
    if not args.simulation:
        key_ready, _, limits_ready = current_state(ROOT)
        if args.configure or not (key_ready and limits_ready):
            setup_result = setup_window(ROOT)
            if setup_result == 'cancel':
                print('启动已取消。', flush=True)
                return 0
            if args.configure:
                print('配置窗口已关闭。已运行的服务需要退出壁纸并停止后台，再重新启动。', flush=True)
                return 0
            if setup_result == 'simulation':
                args.simulation = True
    if args.acceptance and not args.simulation:
        raise ValueError('Acceptance requires simulation')
    from tools.start_standalone import launch
    return launch(simulation=args.simulation, acceptance=args.acceptance, desktop=True, checked=portable)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('启动未完成，请运行检查环境并查看操作说明；未自动测试账号。', flush=True)
        raise SystemExit(1)
