using System;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

internal sealed partial class CompanionWindow : Form
{
    private const string Origin = "http://127.0.0.1:8765";
    private readonly WebView2 view = new WebView2();
    private readonly string root;
    private readonly string evidence;
    private readonly bool acceptance;
    private readonly bool liveAcceptance;
    private readonly string liveStage, liveRun;
    private CoreWebView2Environment environment;
    private bool locked, sleeping, closing, closed;
    private readonly JavaScriptSerializer json = new JavaScriptSerializer();

    [STAThread]
    private static void Main(string[] args)
    {
        bool live = args.Contains("--live-acceptance");
        string stage = args.FirstOrDefault(a => a.StartsWith("--live-stage=")) ?? "";
        string run = args.FirstOrDefault(a => a.StartsWith("--live-run=")) ?? "";
        stage = stage.Replace("--live-stage=", ""); run = run.Replace("--live-run=", "");
        Guid runId;
        if (live && (args.Contains("--acceptance") || (stage != "remember" && stage != "recall") || !Guid.TryParseExact(run, "N", out runId))) { Environment.ExitCode = 1; return; }
        bool testing = args.Contains("--acceptance") || live;
        bool created;
        using (var mutex = new Mutex(true, "Local\\DesktopCompanion.Standalone.v1" + (testing ? ".Acceptance" : ""), out created))
        {
            if (!created) return;
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new CompanionWindow(testing, live, stage, run, args.Contains("--desktop")));
        }
    }

    private CompanionWindow(bool testing, bool live, string stage, string run, bool wallpaper)
    {
        acceptance = testing;
        liveAcceptance = live; liveStage = stage; liveRun = run;
        root = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".."));
        evidence = live ? Path.Combine(root, "evidence", "live-acceptance-" + run, stage) : Path.Combine(root, "evidence", "acceptance-" + DateTime.UtcNow.ToString("yyyyMMdd-HHmmss"));
        Text = live ? "知夏 · 独立运行验证（真实服务）" : testing ? "知夏 · 独立运行验证（模拟）" : "知夏 · 桌面陪伴";
        ClientSize = new Size(1280, 820); MinimumSize = new Size(800, 660);
        StartPosition = FormStartPosition.CenterScreen; AutoScaleMode = AutoScaleMode.Dpi;
        view.Dock = DockStyle.Fill; view.DefaultBackgroundColor = Color.FromArgb(21, 23, 32);
        Controls.Add(view);
        ConfigureDesktop(wallpaper);
        Shown += async delegate { await Initialize(); };
        Resize += async delegate { await UpdatePause(); };
        Microsoft.Win32.SystemEvents.SessionSwitch += SessionSwitch;
        Microsoft.Win32.SystemEvents.PowerModeChanged += PowerChanged;
        FormClosing += async delegate(object sender, FormClosingEventArgs e) {
            if (closed) return;
            e.Cancel = true;
            if (closing) return;
            closing = true;
            if (settingsWindow != null && !settingsWindow.IsDisposed) settingsWindow.Close();
            DisposeDesktop();
            await SetPause(true);
            await Task.Delay(250);
            Microsoft.Win32.SystemEvents.SessionSwitch -= SessionSwitch;
            Microsoft.Win32.SystemEvents.PowerModeChanged -= PowerChanged;
            view.Dispose(); closed = true; Close();
        };
    }

    private static bool Trusted(string address)
    {
        Uri uri;
        return Uri.TryCreate(address, UriKind.Absolute, out uri) &&
            uri.GetLeftPart(UriPartial.Authority) == Origin && uri.UserInfo.Length == 0;
    }

    private Form settingsWindow;
    private async Task OpenSettings()
    {
        if (settingsWindow != null && !settingsWindow.IsDisposed) { settingsWindow.Show(); settingsWindow.Activate(); return; }
        var settings = new Form { Text = "知夏 · 人物与声音设置", Size = new Size(860, 760), StartPosition = FormStartPosition.CenterScreen };
        settingsWindow = settings;
        var child = new WebView2 { Dock = DockStyle.Fill }; settings.Controls.Add(child);
        settings.FormClosed += delegate { child.Dispose(); settingsWindow = null; };
        await child.EnsureCoreWebView2Async(environment); Secure(child);
        child.CoreWebView2.Navigate(Origin + "/admin"); if (desktopMode) settings.Show(); else settings.Show(this);
    }

    private void Secure(WebView2 browser)
    {
        var core = browser.CoreWebView2;
        core.Settings.AreHostObjectsAllowed = false; core.Settings.AreDevToolsEnabled = false;
        core.Settings.AreDefaultContextMenusEnabled = false; core.Settings.AreBrowserAcceleratorKeysEnabled = false;
        core.Settings.IsPasswordAutosaveEnabled = false; core.Settings.IsGeneralAutofillEnabled = false;
        core.Settings.IsStatusBarEnabled = false;
        core.PermissionRequested += delegate(object s, CoreWebView2PermissionRequestedEventArgs e) {
            e.State = CoreWebView2PermissionState.Deny; e.SavesInProfile = false;
            if (closing || locked || sleeping || !Trusted(e.Uri) || e.PermissionKind != CoreWebView2PermissionKind.Microphone) return;
            // The voice module requests a stream only after the user chooses
            // voice conversation. Never grant a persistent browser permission.
            // Return from the WebView2 callback before showing a modal dialog.
            // A deferral avoids entering a nested message loop inside its event.
            var permission = e.GetDeferral();
            BeginInvoke(new Action(delegate {
                try {
                    if (closing || locked || sleeping || browser.IsDisposed) return;
                    var owner = desktopMode && chat != null && chat.Visible ? (IWin32Window)chat : this;
                    if (MessageBox.Show(owner, "语音对话会打开本机默认麦克风，并将分句音频发送到已配置的语音识别服务。允许本次使用麦克风吗？", "知夏 · 本次语音对话", MessageBoxButtons.YesNo, MessageBoxIcon.Question, MessageBoxDefaultButton.Button2) == DialogResult.Yes && !closing && !locked && !sleeping)
                        e.State = CoreWebView2PermissionState.Allow;
                } finally { permission.Complete(); }
            }));
        };
        core.DownloadStarting += delegate(object s, CoreWebView2DownloadStartingEventArgs e) { e.Cancel = true; };
        core.NavigationStarting += delegate(object s, CoreWebView2NavigationStartingEventArgs e) { e.Cancel = !Trusted(e.Uri); };
        core.AddWebResourceRequestedFilter("*", CoreWebView2WebResourceContext.All, CoreWebView2WebResourceRequestSourceKinds.All);
        core.WebResourceRequested += delegate(object s, CoreWebView2WebResourceRequestedEventArgs e) {
            if (!Trusted(e.Request.Uri) && !e.Request.Uri.StartsWith("blob:" + Origin + "/", StringComparison.Ordinal))
                e.Response = environment.CreateWebResourceResponse(new MemoryStream(), 403, "Resource denied", "Content-Type: text/plain\r\n");
        };
        core.NewWindowRequested += async delegate(object s, CoreWebView2NewWindowRequestedEventArgs e) {
            e.Handled = true;
            if (!e.IsUserInitiated || e.Uri != Origin + "/admin") return;
            await OpenSettings();
        };
    }

    private async Task Initialize()
    {
        try {
            var options = new CoreWebView2EnvironmentOptions("--autoplay-policy=no-user-gesture-required --disable-background-networking" + (desktopMode ? " --disable-background-timer-throttling --disable-renderer-backgrounding --disable-backgrounding-occluded-windows" : ""));
            string profile = acceptance ? Path.Combine(evidence, "profile") : Path.Combine(root, "runtime-profile");
            environment = await CoreWebView2Environment.CreateAsync(null, profile, options);
            await view.EnsureCoreWebView2Async(environment);
            if (closing) return;
            Secure(view);
            view.CoreWebView2.ProcessFailed += delegate {
                Text = "知夏 · 显示进程异常，请关闭后重新打开";
                if (acceptance) { SaveFailure("webview-process-failed"); Close(); }
            };
            view.CoreWebView2.NavigationCompleted += async delegate(object s, CoreWebView2NavigationCompletedEventArgs e) {
                if (!e.IsSuccess) { if (acceptance) { SaveFailure("navigation-failed-" + e.WebErrorStatus); Close(); } return; }
                await UpdatePause();
                if (desktopMode && !desktopStarted) {
                    if (!await StartDesktop()) return;
                }
                if (acceptance) await RunAcceptance();
            };
            view.CoreWebView2.Navigate(Origin + "/standalone" + (desktopMode ? "?desktop=1" : ""));
        } catch (Exception error) {
            if (acceptance) { SaveFailure("startup-failed-" + error.GetType().Name + "-" + error.HResult); Close(); }
            else MessageBox.Show(this, "独立窗口未能启动。请确认本地服务已启动、Microsoft Edge WebView2 Runtime 已安装。", "启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    private async Task SetPause(bool value)
    {
        if (view.CoreWebView2 == null || view.IsDisposed) return;
        try { await view.CoreWebView2.ExecuteScriptAsync("window.companionHost?.setSuspended(" + (value ? "true" : "false") + ")"); } catch { }
    }
    private Task UpdatePause() { return SetPause(closing || locked || sleeping || (desktopMode ? desktopPaused || fullscreenPaused || desktopUnavailable : WindowState == FormWindowState.Minimized)); }
    private void SessionSwitch(object sender, Microsoft.Win32.SessionSwitchEventArgs e)
    {
        if (e.Reason == Microsoft.Win32.SessionSwitchReason.SessionLock) locked = true;
        else if (e.Reason == Microsoft.Win32.SessionSwitchReason.SessionUnlock) locked = false;
        if (!IsDisposed && IsHandleCreated) BeginInvoke(new Action(async delegate { await UpdatePause(); }));
    }
    private void PowerChanged(object sender, Microsoft.Win32.PowerModeChangedEventArgs e)
    {
        if (e.Mode == Microsoft.Win32.PowerModes.Suspend) sleeping = true;
        else if (e.Mode == Microsoft.Win32.PowerModes.Resume) sleeping = false;
        if (!IsDisposed && IsHandleCreated) BeginInvoke(new Action(async delegate { await UpdatePause(); }));
    }
    private void SaveFailure(string code)
    {
        Environment.ExitCode = 1;
        Directory.CreateDirectory(evidence);
        File.WriteAllText(Path.Combine(evidence, "result.json"), json.Serialize(new { passed = false, code = code }));
    }
    private async Task RunAcceptance()
    {
        try {
            Directory.CreateDirectory(evidence);
            if (liveAcceptance) {
                // A persisted one-shot marker prevents navigation/relaunch from
                // silently repeating a billable stage. No automatic retries.
                string marker = Path.Combine(evidence, "started.json");
                using (var file = new FileStream(marker, FileMode.CreateNew, FileAccess.Write))
                using (var writer = new StreamWriter(file))
                    writer.Write(json.Serialize(new { stage = liveStage, run = liveRun, time = DateTime.UtcNow.ToString("o") }));
                await view.CoreWebView2.ExecuteScriptAsync("window.standaloneLiveConfig=" + json.Serialize(new { stage = liveStage, run = liveRun }) + ";");
            }
            string script = File.ReadAllText(Path.Combine(root, liveAcceptance ? "live-acceptance.js" : "acceptance.js"));
            await view.CoreWebView2.ExecuteScriptAsync(script);
            for (int i = 0; i < 360 && !closing; i++) {
                await Task.Delay(500);
                string result = await view.CoreWebView2.ExecuteScriptAsync("window.standaloneAcceptanceResult||null");
                if (result == "null") continue;
                var report = json.Deserialize<System.Collections.Generic.Dictionary<string, object>>(result);
                if (!liveAcceptance && report.ContainsKey("passed") && (bool)report["passed"]) {
                    report["nativeLifecycle"] = desktopMode ? await CheckDesktopLifecycle() : await CheckNativeLifecycle();
                    string late = await view.CoreWebView2.ExecuteScriptAsync("window.standaloneAcceptanceTelemetry");
                    var telemetry = json.Deserialize<System.Collections.Generic.Dictionary<string, object>>(late);
                    report["nativeTelemetry"] = telemetry;
                    if (((System.Collections.ICollection)telemetry["errors"]).Count != 0 ||
                        ((System.Collections.ICollection)telemetry["violations"]).Count != 0)
                        throw new InvalidOperationException("late page errors");
                }
                File.WriteAllText(Path.Combine(evidence, "result.json"), json.Serialize(report));
                File.WriteAllText(Path.Combine(evidence, "runtime.txt"), environment.BrowserVersionString);
                using (var screenshot = File.Create(Path.Combine(evidence, "complete.png")))
                    await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, screenshot);
                Environment.ExitCode = report.ContainsKey("passed") && (bool)report["passed"] ? 0 : 1;
                Text = liveAcceptance ? "知夏 · 独立运行验证完成（真实服务）" : "知夏 · 独立运行验证完成（模拟）";
                Close();
                return;
            }
            SaveFailure("acceptance-timeout");
        } catch (Exception error) { SaveFailure("acceptance-runner-failed-" + error.GetType().Name); }
        Close();
    }

    private async Task RequirePage(string expression, string code)
    {
        for (int i = 0; i < 80; i++) {
            if (await view.CoreWebView2.ExecuteScriptAsync(expression) == "true") return;
            await Task.Delay(100);
        }
        throw new InvalidOperationException(code);
    }

    private async Task<object> CheckNativeLifecycle()
    {
        const string pausedPage = "window.companionHost.suspended && document.getElementById('send').disabled && [...document.querySelectorAll('video,audio')].every(v=>v.paused)";
        const string resumedPage = "!window.companionHost.suspended && !document.getElementById('send').disabled && document.getElementById('message').value==='保留的中文草稿'";
        WindowState = FormWindowState.Minimized;
        await RequirePage(pausedPage, "minimize-pause");
        WindowState = FormWindowState.Normal;
        await RequirePage(resumedPage, "restore-reconnect");
        // Invoke this window's real handlers without locking the user's computer.
        SessionSwitch(this, new Microsoft.Win32.SessionSwitchEventArgs(Microsoft.Win32.SessionSwitchReason.SessionLock));
        await RequirePage(pausedPage, "session-lock-handler");
        PowerChanged(this, new Microsoft.Win32.PowerModeChangedEventArgs(Microsoft.Win32.PowerModes.Suspend));
        SessionSwitch(this, new Microsoft.Win32.SessionSwitchEventArgs(Microsoft.Win32.SessionSwitchReason.SessionUnlock));
        await Task.Delay(300);
        await RequirePage(pausedPage, "sleep-retains-pause");
        PowerChanged(this, new Microsoft.Win32.PowerModeChangedEventArgs(Microsoft.Win32.PowerModes.Resume));
        await RequirePage(resumedPage, "power-resume-handler");
        await Task.Delay(1800);
        await RequirePage(resumedPage, "late-close-fence");
        return new { minimizedAndRestored = true, draftPreserved = true,
            sessionAndPowerHandlers = true, realSystemLockOrSleep = false };
    }
}
