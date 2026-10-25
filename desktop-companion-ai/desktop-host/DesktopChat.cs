using System;
using System.Collections;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Runtime.InteropServices;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows.Forms;

// This panel only presents the single wallpaper session. It owns no socket,
// model client or audio player; every command is forwarded to the wallpaper.
internal sealed class DesktopChat : Form
{
    private static readonly Color Plum = Color.FromArgb(45, 32, 41);
    private static readonly Color Surface = Color.FromArgb(61, 44, 54);
    private static readonly Color Ink = Color.FromArgb(246, 234, 229);
    private static readonly Color Dust = Color.FromArgb(194, 168, 178);
    private static readonly Color Rose = Color.FromArgb(157, 91, 114);
    internal readonly TextBox Draft = new TextBox { Multiline = true, MaxLength = 2000, AcceptsReturn = true, BorderStyle = BorderStyle.None, ScrollBars = ScrollBars.None };
    private readonly RichTextBox history = new RichTextBox { ReadOnly = true, BorderStyle = BorderStyle.None, DetectUrls = false, ScrollBars = RichTextBoxScrollBars.None, HideSelection = false, TabStop = false };
    private readonly Label title = new Label { Text = "知夏", AutoSize = false };
    private readonly Label status = new Label { Text = "正在连接…", AutoSize = false, AutoEllipsis = true };
    private readonly Label hint = new Label { Text = "Enter 发送  ·  Shift + Enter 换行", AutoSize = false, TextAlign = ContentAlignment.MiddleRight };
    private readonly Label placeholder = new Label { Text = "想和我说些什么？", AutoSize = false };
    private readonly SoftSurface composer = new SoftSurface { BackColor = Plum, Fill = Surface };
    private readonly SoftButton send = new SoftButton { Text = "发送", Primary = true, Enabled = false };
    private readonly SoftButton voice = new SoftButton { Text = "语音", Enabled = false };
    private readonly SoftButton stop = new SoftButton { Text = "停止" };
    private readonly SoftButton more = new SoftButton { Text = "···", AccessibleName = "更多" };
    private readonly SoftButton hide = new SoftButton { Text = "×", AccessibleName = "收起对话" };
    private readonly ContextMenuStrip menu = new ContextMenuStrip { ShowImageMargin = false, ShowItemToolTips = true };
    private readonly ToolStripMenuItem muteItem = new ToolStripMenuItem("静音");
    private readonly ToolStripMenuItem usageItem = new ToolStripMenuItem("今日用量");
    private readonly ToolTip tips = new ToolTip();
    private readonly Func<string, object, Task<bool>> command;
    private readonly Font bodyFont = new Font("Microsoft YaHei UI", 10.5f);
    private readonly Font smallFont = new Font("Microsoft YaHei UI", 8.5f);
    private readonly Font titleFont = new Font("Microsoft YaHei UI", 16f, FontStyle.Bold);
    private bool updating, sending, sized;
    private float scale = 1;
    private string lastHistory, lastLimits;
    internal bool AllowClose;

    internal DesktopChat(Func<string, object, Task<bool>> callback)
    {
        command = callback; SuspendLayout();
        Text = "知夏 · 壁纸对话"; FormBorderStyle = FormBorderStyle.None;
        StartPosition = FormStartPosition.Manual; ShowInTaskbar = true;
        AutoScaleMode = AutoScaleMode.None; BackColor = Plum; ForeColor = Ink;
        Font = bodyFont; Opacity = 0.89; DoubleBuffered = true; KeyPreview = true;
        title.Font = titleFont; title.ForeColor = Ink;
        status.Font = hint.Font = smallFont; status.ForeColor = hint.ForeColor = Dust;
        history.BackColor = Plum; history.ForeColor = Ink; history.Font = bodyFont;
        Draft.BackColor = Surface; Draft.ForeColor = Ink; Draft.Font = bodyFont;
        Draft.AccessibleName = "消息";
        placeholder.BackColor = Surface; placeholder.ForeColor = Dust; placeholder.Font = bodyFont;
        composer.Controls.Add(Draft); composer.Controls.Add(placeholder); placeholder.BringToFront();
        Controls.AddRange(new Control[] { title, status, history, composer, hint, voice, stop, send, more, hide });
        foreach (Control control in new Control[] { title, status }) control.MouseDown += DragHeader;
        placeholder.Click += delegate { Draft.Focus(); };
        menu.BackColor = Surface; menu.ForeColor = Ink; menu.Font = new Font("Microsoft YaHei UI", 9.5f);
        menu.Renderer = new PlumRenderer();
        menu.Items.Add("人物与声音", null, async delegate { await command("settings", null); });
        menu.Items.Add("重试末条语音", null, async delegate { await command("retry", null); });
        muteItem.Click += async delegate { await command("mute", null); }; menu.Items.Add(muteItem);
        var volume = new ToolStripMenuItem("音量");
        foreach (int percent in new[] { 25, 50, 75, 100 }) {
            int value = percent;
            volume.DropDownItems.Add(percent + "%", null, async delegate { await command("volume", value / 100.0); });
        }
        menu.Items.Add(volume); menu.Items.Add(new ToolStripSeparator()); menu.Items.Add(usageItem);
        var wallpaper = new ToolStripMenuItem("壁纸");
        wallpaper.DropDownItems.Add("暂停 / 继续", null, async delegate { await command("pauseWallpaper", null); });
        wallpaper.DropDownItems.Add("重新附着桌面", null, async delegate { await command("reattach", null); });
        wallpaper.DropDownItems.Add("退出壁纸", null, async delegate { await command("exit", null); });
        menu.Items.Add(wallpaper); ThemeMenu(menu.Items);
        more.Click += delegate { menu.Show(more, new Point(more.Width - menu.PreferredSize.Width, more.Height + U(6))); };
        hide.Click += delegate { HidePanel(); };
        send.Click += async delegate { await SendDraft(); };
        stop.Click += async delegate { await command("stop", null); };
        voice.Click += async delegate { await command("voice", null); };
        Draft.TextChanged += async delegate {
            placeholder.Visible = Draft.TextLength == 0;
            if (!updating) await command("draft", Draft.Text);
        };
        Draft.KeyDown += async delegate(object sender, KeyEventArgs e) {
            if (e.KeyCode == Keys.Enter && !e.Shift && !Composing()) { e.SuppressKeyPress = true; await SendDraft(); }
        };
        KeyDown += delegate(object sender, KeyEventArgs e) {
            if (e.KeyCode == Keys.Escape && !Composing()) { e.SuppressKeyPress = true; HidePanel(); }
        };
        FormClosing += delegate(object sender, FormClosingEventArgs e) {
            if (!AllowClose) { e.Cancel = true; HidePanel(); }
        };
        Shown += delegate { Draft.Focus(); };
        tips.SetToolTip(more, "更多 · 用量、声音与壁纸"); tips.SetToolTip(hide, "收起对话 · Esc");
        tips.SetToolTip(voice, "语音对话"); tips.SetToolTip(stop, "停止当前回复与声音");
        ResumeLayout(true);
    }
    private void ThemeMenu(ToolStripItemCollection items)
    {
        foreach (ToolStripItem item in items) {
            item.ForeColor = Ink; item.BackColor = Surface;
            var child = item as ToolStripMenuItem;
            if (child != null) {
                child.DropDown.BackColor = Surface; child.DropDown.ForeColor = Ink;
                var dropdown = child.DropDown as ToolStripDropDownMenu;
                if (dropdown != null) dropdown.ShowImageMargin = false;
                ThemeMenu(child.DropDownItems);
            }
        }
    }
    private int U(int value) { return (int)Math.Round(value * scale); }
    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e); scale = Math.Max(1f, GetDpiForWindow(Handle) / 96f);
        MinimumSize = new Size(U(360), U(460));
        if (!sized) { sized = true; ClientSize = new Size(U(420), U(566)); }
        PerformLayout();
    }
    protected override void OnLayout(LayoutEventArgs e)
    {
        base.OnLayout(e); if (composer == null || title == null) return;
        int w = ClientSize.Width, h = ClientSize.Height, pad = U(24), content = Math.Max(U(120), w - pad * 2);
        title.SetBounds(pad, U(20), w - U(140), U(38));
        status.SetBounds(pad + U(1), U(63), content, U(24));
        more.SetBounds(w - U(101), U(24), U(34), U(34)); hide.SetBounds(w - U(59), U(24), U(34), U(34));
        composer.SetBounds(pad, h - U(172), content, U(96));
        history.SetBounds(pad, U(107), content, Math.Max(U(50), composer.Top - U(130)));
        Draft.SetBounds(U(14), U(13), Math.Max(U(80), composer.Width - U(28)), U(69));
        placeholder.SetBounds(Draft.Left, Draft.Top + U(1), Draft.Width, U(28));
        voice.SetBounds(pad, h - U(65), U(74), U(36));
        stop.SetBounds(pad + U(82), h - U(65), U(60), U(36));
        send.SetBounds(w - pad - U(82), h - U(65), U(82), U(36));
        hint.SetBounds(pad, h - U(25), content, U(18));
        if (w > 0 && h > 0) {
            using (var path = Rounded(new Rectangle(0, 0, w, h), U(20))) {
                Region old = Region; Region = new Region(path); if (old != null) old.Dispose();
            }
        }
    }
    protected override void OnMouseDown(MouseEventArgs e) { base.OnMouseDown(e); if (e.Y < U(97)) DragHeader(this, e); }
    private void DragHeader(object sender, MouseEventArgs e)
    {
        if (e.Button != MouseButtons.Left) return;
        ReleaseCapture(); SendMessage(Handle, 0xA1, new IntPtr(2), IntPtr.Zero);
    }
    protected override void WndProc(ref Message m)
    {
        base.WndProc(ref m);
        if (m.Msg != 0x84 || m.Result.ToInt32() != 1) return;
        long pos = m.LParam.ToInt64(); Point point = PointToClient(new Point((short)(pos & 0xffff), (short)((pos >> 16) & 0xffff)));
        int edge = U(7); bool left = point.X < edge, right = point.X >= Width - edge, top = point.Y < edge, bottom = point.Y >= Height - edge;
        if (top) m.Result = new IntPtr(left ? 13 : right ? 14 : 12);
        else if (bottom) m.Result = new IntPtr(left ? 16 : right ? 17 : 15);
        else if (left || right) m.Result = new IntPtr(left ? 10 : 11);
    }
    internal void PlaceOnScreen()
    {
        IntPtr handle = Handle;
        Rectangle area = Screen.PrimaryScreen.WorkingArea;
        Width = Math.Min(Width, area.Width - U(32)); Height = Math.Min(Height, area.Height - U(32));
        Location = new Point(area.Right - Width - U(20), area.Bottom - Height - U(20));
    }
    internal bool LayoutVerified()
    {
        PerformLayout(); Rectangle visible = RectangleToScreen(ClientRectangle);
        foreach (Control control in new Control[] { Draft, history, send, voice, stop, more, hide })
            if (!control.Visible || control.Width <= 0 || control.Height <= 0 || !visible.Contains(control.RectangleToScreen(control.ClientRectangle))) return false;
        return FormBorderStyle == FormBorderStyle.None && Opacity < 1 && Draft.BorderStyle == BorderStyle.None;
    }
    private bool Composing()
    {
        if (!Draft.IsHandleCreated) return false;
        IntPtr context = ImmGetContext(Draft.Handle); if (context == IntPtr.Zero) return false;
        try { return ImmGetCompositionString(context, 8, IntPtr.Zero, 0) > 0; }
        finally { ImmReleaseContext(Draft.Handle, context); }
    }
    internal async Task SendDraft()
    {
        if (sending || !send.Enabled || String.IsNullOrWhiteSpace(Draft.Text)) return;
        string draft = Draft.Text; sending = true; send.Enabled = false;
        try {
            bool accepted = await command("send", draft);
            if (accepted && Draft.Text == draft) { updating = true; Draft.Clear(); updating = false; }
            if (!accepted) status.Text = "连接暂未恢复，草稿已保留";
        } finally { sending = false; }
    }
    internal async void HidePanel() { Hide(); await command("stopVoice", null); }
    internal void SetSnapshot(string text, string state, string limits, bool ready, bool voiceReady, string voiceLabel, bool muted, object turns = null)
    {
        if (lastHistory != text) { RenderHistory(text, turns); lastHistory = text; }
        status.Text = state == "就绪" ? "在线 · 陪你聊一会儿" : state;
        tips.SetToolTip(status, state); send.Enabled = ready && !sending; voice.Enabled = voiceReady;
        voice.Text = !String.IsNullOrEmpty(voiceLabel) && (voiceLabel.Contains("停止") || voiceLabel.Contains("结束")) ? "结束语音" : "语音";
        tips.SetToolTip(voice, voiceLabel); muteItem.Text = muted ? "取消静音" : "静音"; muteItem.Checked = muted;
        limits = limits ?? "";
        usageItem.ToolTipText = limits;
        var texts = Regex.Match(limits, @"今日文字\s*(\d+)"); var voices = Regex.Match(limits, @"语音\s*(\d+)");
        usageItem.Text = texts.Success && voices.Success ? "今日 · 文字 " + texts.Groups[1].Value + " / 语音 " + voices.Groups[1].Value : "今日用量";
        if (lastLimits != limits) {
            lastLimits = limits; usageItem.DropDownItems.Clear();
            foreach (string line in new[] { usageItem.Text, "尝试次数包含失败或取消", "按北京时间每日统计", "不是账单、余额或免费额度" })
                usageItem.DropDownItems.Add(new ToolStripMenuItem(line) { Enabled = false, ToolTipText = limits });
            ThemeMenu(usageItem.DropDownItems);
        }
    }
    private void RenderHistory(string fallback, object turns)
    {
        history.Clear(); var rows = turns as IEnumerable;
        if (rows == null) { history.Text = fallback ?? ""; }
        else foreach (object item in rows) {
            var row = item as IDictionary<string, object>; if (row == null) continue;
            string user = GetText(row, "user"), reply = GetText(row, "reply"), meta = GetText(row, "meta");
            if (user.Length > 0) { Append("你\n", smallFont, Dust, HorizontalAlignment.Right); Append(user + "\n\n", bodyFont, Dust, HorizontalAlignment.Right); }
            if (reply.Length > 0) { Append("知夏\n", smallFont, Dust, HorizontalAlignment.Left); Append(reply + "\n\n", bodyFont, Ink, HorizontalAlignment.Left); }
            if (meta.Length > 0 && (meta.Contains("失败") || meta.Contains("取消") || meta.Contains("中断") || meta.Contains("错误") || meta.Contains("故障"))) Append(meta + "\n\n", smallFont, Dust, HorizontalAlignment.Left);
        }
        history.SelectionStart = history.TextLength; history.SelectionLength = 0; history.ScrollToCaret();
    }
    private static string GetText(IDictionary<string, object> row, string key) { return row.ContainsKey(key) && row[key] != null ? row[key].ToString().Trim() : ""; }
    private void Append(string text, Font font, Color color, HorizontalAlignment alignment)
    {
        history.SelectionStart = history.TextLength; history.SelectionLength = 0;
        history.SelectionFont = font; history.SelectionColor = color; history.SelectionAlignment = alignment;
        history.AppendText(text);
    }
    protected override void Dispose(bool disposing)
    {
        base.Dispose(disposing);
        if (disposing) { menu.Dispose(); tips.Dispose(); bodyFont.Dispose(); smallFont.Dispose(); titleFont.Dispose(); }
    }
    private static GraphicsPath Rounded(Rectangle rectangle, int radius)
    {
        var path = new GraphicsPath(); int diameter = Math.Min(radius * 2, Math.Min(rectangle.Width, rectangle.Height));
        path.AddArc(rectangle.X, rectangle.Y, diameter, diameter, 180, 90); path.AddArc(rectangle.Right - diameter, rectangle.Y, diameter, diameter, 270, 90);
        path.AddArc(rectangle.Right - diameter, rectangle.Bottom - diameter, diameter, diameter, 0, 90); path.AddArc(rectangle.X, rectangle.Bottom - diameter, diameter, diameter, 90, 90); path.CloseFigure(); return path;
    }
    private sealed class SoftSurface : Panel
    {
        internal Color Fill;
        internal SoftSurface() { DoubleBuffered = true; }
        protected override void OnPaint(PaintEventArgs e) { e.Graphics.SmoothingMode = SmoothingMode.AntiAlias; using (var path = Rounded(ClientRectangle, Math.Max(8, Height / 8))) using (var brush = new SolidBrush(Fill)) e.Graphics.FillPath(brush, path); }
    }
    private sealed class SoftButton : Button
    {
        internal bool Primary;
        private bool hovering;
        internal SoftButton() { SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, true); FlatStyle = FlatStyle.Flat; FlatAppearance.BorderSize = 0; Cursor = Cursors.Hand; BackColor = Plum; ForeColor = Dust; }
        protected override void OnMouseEnter(EventArgs e) { hovering = true; Invalidate(); base.OnMouseEnter(e); }
        protected override void OnMouseLeave(EventArgs e) { hovering = false; Invalidate(); base.OnMouseLeave(e); }
        protected override void OnGotFocus(EventArgs e) { Invalidate(); base.OnGotFocus(e); }
        protected override void OnLostFocus(EventArgs e) { Invalidate(); base.OnLostFocus(e); }
        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.Clear(Plum); e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            Color fill = Primary ? (Enabled ? (hovering ? Color.FromArgb(181, 113, 137) : Rose) : Surface) : (hovering || Focused ? Surface : Plum);
            using (var path = Rounded(ClientRectangle, Math.Max(6, Height / 4))) using (var brush = new SolidBrush(fill)) e.Graphics.FillPath(brush, path);
            TextRenderer.DrawText(e.Graphics, Text, Font, ClientRectangle, Enabled ? (Primary ? Ink : Dust) : Color.FromArgb(128, 106, 118), TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter);
        }
    }
    private sealed class PlumRenderer : ToolStripProfessionalRenderer
    {
        protected override void OnRenderToolStripBackground(ToolStripRenderEventArgs e) { e.Graphics.Clear(Surface); }
        protected override void OnRenderToolStripBorder(ToolStripRenderEventArgs e) { }
        protected override void OnRenderImageMargin(ToolStripRenderEventArgs e) { using (var brush = new SolidBrush(Surface)) e.Graphics.FillRectangle(brush, e.AffectedBounds); }
        protected override void OnRenderMenuItemBackground(ToolStripItemRenderEventArgs e) { if (e.Item.Selected) using (var brush = new SolidBrush(Color.FromArgb(89, 58, 74))) e.Graphics.FillRectangle(brush, new Rectangle(Point.Empty, e.Item.Size)); }
        protected override void OnRenderItemText(ToolStripItemTextRenderEventArgs e) { e.TextColor = e.Item.Enabled ? Ink : Dust; base.OnRenderItemText(e); }
        protected override void OnRenderSeparator(ToolStripSeparatorRenderEventArgs e) { using (var pen = new Pen(Color.FromArgb(86, 62, 76))) e.Graphics.DrawLine(pen, 12, e.Item.Height / 2, e.Item.Width - 12, e.Item.Height / 2); }
    }
    [DllImport("user32.dll")] private static extern uint GetDpiForWindow(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern bool ReleaseCapture();
    [DllImport("user32.dll")] private static extern IntPtr SendMessage(IntPtr hwnd, int message, IntPtr wparam, IntPtr lparam);
    [DllImport("imm32.dll")] private static extern IntPtr ImmGetContext(IntPtr hwnd);
    [DllImport("imm32.dll")] private static extern bool ImmReleaseContext(IntPtr hwnd, IntPtr context);
    [DllImport("imm32.dll", CharSet = CharSet.Unicode)] private static extern int ImmGetCompositionString(IntPtr context, uint index, IntPtr buffer, uint length);
}
