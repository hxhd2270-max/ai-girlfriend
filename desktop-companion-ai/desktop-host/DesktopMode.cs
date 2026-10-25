using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using System.Windows.Forms;

internal sealed partial class CompanionWindow
{
    private bool desktopMode, desktopStarted, desktopPaused, fullscreenPaused, desktopUnavailable, desktopBusy, desktopSwitching;
    private IntPtr hotkeyWindow;
    private DesktopSession desktop;
    private DesktopChat chat;
    private NotifyIcon tray;
    private readonly System.Windows.Forms.Timer desktopTimer = new System.Windows.Forms.Timer { Interval = 650 };
    private readonly List<object> desktopEvents = new List<object>();
    private IntPtr mouseHook;
    private MouseHookProc mouseCallback;
    private uint taskbarCreated;
    private int recoveryAttempts;
    private const int ChatHotkey = 0x4358;

    private void ConfigureDesktop(bool enabled)
    {
        desktopMode = enabled; if (!enabled) return;
        Text = "知夏 · 独立壁纸";
        taskbarCreated = RegisterWindowMessage("TaskbarCreated");
        desktopTimer.Tick += DesktopTick;
    }
    private async Task<bool> StartDesktop()
    {
        desktopStarted = true;
        desktopSwitching = true;
        try {
            desktop = new DesktopSession(this); desktop.Attach();
            chat = new DesktopChat(ChatCommand);
            chat.PlaceOnScreen();
            var menu = new ContextMenuStrip();
            menu.Items.Add("和知夏聊天（Ctrl+Alt+C）", null, delegate { ShowChat(); });
            menu.Items.Add("暂停 / 继续", null, async delegate { desktopPaused = !desktopPaused; await UpdatePause(); SaveDesktopState("pause-toggle"); });
            menu.Items.Add("重新附着桌面", null, async delegate { await RecoverDesktop(); });
            menu.Items.Add("退出壁纸", null, delegate { Close(); });
            tray = new NotifyIcon { Icon = SystemIcons.Application, Text = "知夏 · 独立壁纸", ContextMenuStrip = menu, Visible = true };
            tray.DoubleClick += delegate { ShowChat(); };
            bool hotkey = BindDesktopHotkey();
            mouseCallback = MouseHook;
            using (var process = System.Diagnostics.Process.GetCurrentProcess())
            using (var module = process.MainModule)
                mouseHook = SetWindowsHookEx(14, mouseCallback, GetModuleHandle(module.ModuleName), 0);
            view.CoreWebView2.WebMessageReceived += delegate(object sender, Microsoft.Web.WebView2.Core.CoreWebView2WebMessageReceivedEventArgs e) {
                if (e.Source != Origin + "/standalone?desktop=1") return;
                try { if (e.TryGetWebMessageAsString() == "open-chat") ShowChat(); } catch { }
            };
            SaveDesktopState("attached", new { topology = desktop.Describe(), hotkey = hotkey, desktopClick = mouseHook != IntPtr.Zero });
            desktopTimer.Start(); await PlaceLauncher(); await UpdatePause();
            return true;
        } catch (Exception error) {
            SaveDesktopState("attach-failed", new { code = error.GetType().Name, message = error.Message });
            if (acceptance) SaveFailure("desktop-attach-" + error.Message);
            else MessageBox.Show("壁纸层未能启动：" + error.Message, "知夏", MessageBoxButtons.OK, MessageBoxIcon.Error);
            Close(); return false;
        } finally { desktopSwitching = false; }
    }
    private void ShowChat()
    {
        if (closing || locked || sleeping || chat == null) return;
        if (!chat.Visible) chat.Show();
        if (chat.WindowState == FormWindowState.Minimized) chat.WindowState = FormWindowState.Normal;
        chat.Activate(); chat.Draft.Focus();
    }
    private async Task<bool> ChatCommand(string command, object value)
    {
        if (closing || locked || sleeping || desktopUnavailable || view.CoreWebView2 == null) return false;
        try {
            if (command == "exit") { Close(); return true; }
            if (command == "reattach") { await RecoverDesktop(); return !desktopUnavailable; }
            if (command == "pauseWallpaper") { desktopPaused = !desktopPaused; await UpdatePause(); SaveDesktopState("pause-toggle"); return true; }
            if (command == "settings") { await OpenSettings(); return true; }
            if (!new[] { "draft", "send", "stop", "stopVoice", "voice", "mute", "volume", "retry" }.ContainsItem(command)) return false;
            return await view.CoreWebView2.ExecuteScriptAsync("window.companionDesktop?.command(" + json.Serialize(command) + "," + json.Serialize(value) + ")") == "true";
        } catch { SaveDesktopState("chat-command-failed", new { command = command }); return false; }
    }
    private async void DesktopTick(object sender, EventArgs e)
    {
        if (desktopBusy || desktopSwitching || closing || view.CoreWebView2 == null) return;
        desktopBusy = true;
        try {
            if (desktop == null || !desktop.IsHealthy()) { await RecoverDesktop(); return; }
            desktop.Place();
            bool covered = DesktopSession.IsForeignFullscreen(Handle, chat == null ? IntPtr.Zero : chat.Handle);
            if (covered != fullscreenPaused) { fullscreenPaused = covered; await UpdatePause(); SaveDesktopState("fullscreen", new { paused = covered }); }
            if (chat != null && chat.Visible) {
                var state = json.Deserialize<Dictionary<string, object>>(await view.CoreWebView2.ExecuteScriptAsync("window.companionDesktop?.snapshot()||{}"));
                if (state.ContainsKey("history")) chat.SetSnapshot((string)state["history"], (string)state["status"], (string)state["usage"], (bool)state["ready"], (bool)state["voiceReady"], (string)state["voiceLabel"], (bool)state["muted"], state.ContainsKey("turns") ? state["turns"] : null);
            }
        } catch (Exception error) { SaveDesktopState("tick-error", new { code = error.GetType().Name }); }
        finally { desktopBusy = false; }
    }
    private async Task RecoverDesktop()
    {
        if (closing || locked || sleeping || desktopSwitching) return;
        desktopSwitching = true;
        try {
            desktopUnavailable = true; await UpdatePause();
            if (desktop != null) desktop.Detach();
            desktop = new DesktopSession(this); desktop.Attach();
            bool hotkey = BindDesktopHotkey();
            desktopUnavailable = false; recoveryAttempts = 0; desktopTimer.Start(); await PlaceLauncher(); await UpdatePause();
            SaveDesktopState("reattached", new { topology = desktop.Describe(), hotkey = hotkey });
        } catch (Exception error) {
            recoveryAttempts++; SaveDesktopState("recovery-wait", new { attempts = recoveryAttempts, code = error.GetType().Name });
            if (recoveryAttempts >= 10) { desktopTimer.Stop(); if (tray != null) { tray.BalloonTipTitle = "知夏壁纸已暂停"; tray.BalloonTipText = "桌面暂不可用，可在托盘选择重新附着桌面。"; tray.ShowBalloonTip(5000); } }
        } finally { desktopSwitching = false; }
    }
    private Task PlaceLauncher()
    {
        Rectangle screen = Screen.PrimaryScreen.Bounds, work = Screen.PrimaryScreen.WorkingArea;
        // Web coordinates are CSS pixels; WinForms/Win32 bounds are physical pixels.
        return view.CoreWebView2.ExecuteScriptAsync("(()=>{const s=window.innerWidth/" + screen.Width + ";window.companionDesktop?.placeLauncher(" + (work.Right-screen.Left-178) + "*s," + (work.Bottom-screen.Top-70) + "*s,160*s,52*s)})()");
    }
    private void DisposeDesktop()
    {
        if (!desktopMode) return;
        desktopTimer.Stop(); UnbindDesktopHotkey();
        if (mouseHook != IntPtr.Zero) { UnhookWindowsHookEx(mouseHook); mouseHook = IntPtr.Zero; }
        if (tray != null) { tray.Visible = false; tray.Dispose(); }
        if (chat != null) { chat.AllowClose = true; chat.Close(); chat.Dispose(); }
        if (desktop != null) { desktop.Detach(); desktop = null; }
        SaveDesktopState("detached-and-exited");
    }
    private void SaveDesktopState(string kind, object detail = null)
    {
        desktopEvents.Add(new { utc = DateTime.UtcNow.ToString("o"), kind = kind, detail = detail });
        if (desktopEvents.Count > 120) desktopEvents.RemoveAt(0);
        string path = acceptance ? evidence : Path.Combine(root, "desktop-state");
        try { Directory.CreateDirectory(path); File.WriteAllText(Path.Combine(path, "desktop.json"), json.Serialize(new { events = desktopEvents, desktopMode = desktopMode, primaryOnly = true })); } catch (IOException) { }
    }
    private async Task<object> CheckDesktopLifecycle()
    {
        desktopTimer.Stop(); fullscreenPaused = false; await UpdatePause();
        if (!desktop.IsHealthy()) throw new InvalidOperationException("desktop parent lost");
        var initial = desktop.Describe();
        ShowChat(); await Task.Delay(400);
        if (!chat.LayoutVerified()) throw new InvalidOperationException("native panel controls are clipped");
        await RequirePage("!document.getElementById('send').disabled", "native-panel-ready");
        chat.SetSnapshot("", "本地验收", "", true, false, "语音对话", false);
        chat.Draft.Text = "原生面板发送验收";
        await chat.SendDraft();
        await RequirePage("document.getElementById('history').textContent.includes('原生面板发送验收') && [...document.querySelectorAll('#history .turn')].at(-1)?.querySelector('.meta')?.textContent.includes('已完成')", "native-send-playback");
        if (chat.Draft.Text.Length != 0) throw new InvalidOperationException("native send did not clear acknowledged draft");
        chat.Draft.Text = "原生中文输入草稿";
        await Task.Delay(300);
        await RequirePage("document.getElementById('message').value==='原生中文输入草稿'", "native-draft-bridge");
        chat.HidePanel();
        await RequirePage("!window.companionHost.suspended && !document.getElementById('send').disabled", "panel-hide-keeps-wallpaper");
        await view.CoreWebView2.ExecuteScriptAsync("document.getElementById('message').value='保留的中文草稿'");
        SessionSwitch(this, new Microsoft.Win32.SessionSwitchEventArgs(Microsoft.Win32.SessionSwitchReason.SessionLock));
        await RequirePage("window.companionHost.suspended && document.getElementById('player').paused", "desktop-lock");
        SessionSwitch(this, new Microsoft.Win32.SessionSwitchEventArgs(Microsoft.Win32.SessionSwitchReason.SessionUnlock));
        await RequirePage("!document.getElementById('send').disabled", "desktop-unlock");
        await RecoverDesktop();
        if (!desktop.IsHealthy()) throw new InvalidOperationException("desktop reattach failed");
        if (hotkeyWindow != Handle) throw new InvalidOperationException("hotkey not rebound");
        await RequirePage("!document.getElementById('send').disabled", "desktop-reattach-reconnect");
        await Task.Delay(1800);
        return new { parentVerified = true, initialTopology = initial, reattachVerified = true, hotkeyRegisteredAfterRecovery = true, nativePanelLayoutVerified = true, nativePanelDraft = true, nativePanelSendPlayback = true, hideKeepsPlayback = true, realSystemLockOrSleep = false, sessionHandlersInjected = true, winDManuallyVerified = false };
    }
    private bool BindDesktopHotkey()
    {
        if (hotkeyWindow == Handle) return true;
        UnbindDesktopHotkey();
        if (!RegisterHotKey(Handle, ChatHotkey, 0x4003, (uint)Keys.C)) return false;
        hotkeyWindow = Handle; return true;
    }
    private void UnbindDesktopHotkey()
    {
        if (hotkeyWindow != IntPtr.Zero) UnregisterHotKey(hotkeyWindow, ChatHotkey);
        hotkeyWindow = IntPtr.Zero;
    }
    protected override void OnHandleDestroyed(EventArgs e)
    {
        UnbindDesktopHotkey(); base.OnHandleDestroyed(e);
    }
    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        if (desktopStarted && !desktopSwitching && !closing) BindDesktopHotkey();
    }
    protected override bool ShowWithoutActivation { get { return desktopMode; } }
    protected override void WndProc(ref Message m)
    {
        if (desktopMode && !closing) {
            if (m.Msg == 0x0312 && m.WParam.ToInt32() == ChatHotkey) { ShowChat(); return; }
            if (m.Msg == 0x21) { m.Result = new IntPtr(3); return; }
            if (desktopStarted && !desktopSwitching && (m.Msg == 0x7e || m.Msg == 0x2e0 || (taskbarCreated != 0 && m.Msg == taskbarCreated)))
                BeginInvoke(new Action(async delegate { await RecoverDesktop(); }));
        }
        base.WndProc(ref m);
    }
    private IntPtr MouseHook(int code, IntPtr wparam, IntPtr lparam)
    {
        // Observe only a click in a small visible launch region. All mouse input
        // is passed through; icons and desktop context menus remain Explorer's.
        if (code >= 0 && wparam.ToInt32() == 0x0202 && !locked && !sleeping) {
            var point = (MousePoint)Marshal.PtrToStructure(lparam, typeof(MousePoint));
            Rectangle area = Screen.PrimaryScreen.WorkingArea;
            var region = new Rectangle(area.Right - 178, area.Bottom - 70, 160, 52);
            if (region.Contains(point.X, point.Y) && DesktopSession.IsBlankDesktopPoint(point.X, point.Y))
                BeginInvoke(new Action(ShowChat));
        }
        return CallNextHookEx(mouseHook, code, wparam, lparam);
    }
    [StructLayout(LayoutKind.Sequential)] private struct MousePoint { public int X, Y; public uint MouseData, Flags, Time; public IntPtr Extra; }
    private delegate IntPtr MouseHookProc(int code, IntPtr wparam, IntPtr lparam);
    [DllImport("user32.dll")] private static extern IntPtr SetWindowsHookEx(int id, MouseHookProc proc, IntPtr module, uint thread);
    [DllImport("user32.dll")] private static extern bool UnhookWindowsHookEx(IntPtr hook);
    [DllImport("user32.dll")] private static extern IntPtr CallNextHookEx(IntPtr hook, int code, IntPtr wparam, IntPtr lparam);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] private static extern IntPtr GetModuleHandle(string module);
    [DllImport("user32.dll")] private static extern bool RegisterHotKey(IntPtr hwnd, int id, uint modifiers, uint key);
    [DllImport("user32.dll")] private static extern bool UnregisterHotKey(IntPtr hwnd, int id);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern uint RegisterWindowMessage(string name);
}

internal static class DesktopCommands
{
    internal static bool ContainsItem(this string[] commands, string value) { return Array.IndexOf(commands, value) >= 0; }
}
