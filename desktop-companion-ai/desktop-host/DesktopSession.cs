using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;

// Own-window integration only. Never hides, destroys or restyles Explorer's windows.
internal sealed class DesktopSession
{
    private readonly Form form;
    private Rectangle previousBounds;
    private IntPtr parent, icons, progman;
    private bool attached, raised;
    internal bool Attached { get { return attached; } }
    private uint explorerPid;
    internal DesktopSession(Form value) { form = value; }

    internal static void CheckConflict()
    {
        foreach (string name in new[] { "wallpaper64", "wallpaper32", "lively", "livelywpf" })
            foreach (Process process in Process.GetProcessesByName(name))
                using (process) { throw new InvalidOperationException("A wallpaper host is running: " + name + ". Exit it before attaching this wallpaper."); }
    }

    internal void Attach()
    {
        CheckConflict();
        progman = Native.FindWindow("Progman", null);
        if (progman == IntPtr.Zero) throw new InvalidOperationException("Explorer desktop was not found.");
        UIntPtr result;
        if (Native.SendMessageTimeout(progman, 0x052c, new IntPtr(0xd), new IntPtr(1), 2, 1000, out result) == IntPtr.Zero)
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Desktop preparation timed out.");
        raised = (Native.GetWindowLongPtr(progman, -20).ToInt64() & 0x00200000L) != 0;
        if (raised) {
            icons = Native.FindWindowEx(progman, IntPtr.Zero, "SHELLDLL_DefView", null);
            parent = progman;
        } else {
            Native.EnumWindows(delegate(IntPtr window, IntPtr state) {
                IntPtr found = Native.FindWindowEx(window, IntPtr.Zero, "SHELLDLL_DefView", null);
                if (found != IntPtr.Zero) {
                    icons = found;
                    parent = Native.FindWindowEx(IntPtr.Zero, window, "WorkerW", null);
                    return false;
                }
                return true;
            }, IntPtr.Zero);
        }
        if (parent == IntPtr.Zero || icons == IntPtr.Zero) throw new InvalidOperationException("Unsupported desktop window topology; no attachment attempted.");
        uint iconsPid;
        Native.GetWindowThreadProcessId(icons, out iconsPid);
        Native.GetWindowThreadProcessId(parent, out explorerPid);
        using (var process = Process.GetProcessById((int)explorerPid))
            if (!String.Equals(process.ProcessName, "explorer", StringComparison.OrdinalIgnoreCase) || iconsPid != explorerPid)
                throw new InvalidOperationException("Desktop windows do not belong to the same Explorer process.");
        if (previousBounds.Width == 0) previousBounds = form.Bounds;
        form.Hide();
        form.WindowState = FormWindowState.Normal;
        form.FormBorderStyle = FormBorderStyle.None;
        form.ShowInTaskbar = false;
        IntPtr hwnd = form.Handle;
        long style = Native.GetWindowLongPtr(hwnd, -16).ToInt64();
        Native.SetWindowLongPtr(hwnd, -16, new IntPtr((style & ~0x80000000L) | 0x40000000L));
        long ex = Native.GetWindowLongPtr(hwnd, -20).ToInt64() | 0x08000080L;
        if (raised) ex |= 0x00080000L;
        Native.SetWindowLongPtr(hwnd, -20, new IntPtr(ex & ~0x00040000L));
        Native.SetLastError(0);
        Native.SetParent(hwnd, parent);
        if (Native.GetParent(hwnd) != parent) throw new Win32Exception(Marshal.GetLastWin32Error(), "SetParent did not attach the window.");
        attached = true;
        if (raised && !Native.SetLayeredWindowAttributes(hwnd, 0, 255, 2)) throw new Win32Exception();
        Place();
        // Keep WinForms visibility in sync; the host overrides ShowWithoutActivation.
        form.Show();
        Native.ShowWindow(hwnd, 4);
        Native.SendMessage(hwnd, 0x0127, new IntPtr(0x00030002), IntPtr.Zero);
    }

    internal void Place()
    {
        if (!attached || !Native.IsWindow(parent)) return;
        IntPtr hwnd = form.Handle;
        Rectangle bounds = Screen.PrimaryScreen.Bounds;
        Native.POINT point = new Native.POINT { X = bounds.X, Y = bounds.Y };
        if (!Native.ScreenToClient(parent, ref point)) throw new Win32Exception();
        if (!Native.SetWindowPos(hwnd, raised ? icons : IntPtr.Zero, point.X, point.Y, bounds.Width, bounds.Height, 0x10 | 0x20 | 0x40))
            throw new Win32Exception();
    }

    internal bool IsHealthy()
    {
        uint current;
        Native.GetWindowThreadProcessId(parent, out current);
        return attached && Native.IsWindow(parent) && Native.IsWindow(icons) && Native.IsWindow(form.Handle)
            && Native.GetParent(form.Handle) == parent && current == explorerPid;
    }

    internal object Describe()
    {
        return new { raisedDesktop = raised, parentClass = ClassName(parent), parent = parent.ToInt64(),
            iconView = icons.ToInt64(), ownWindow = form.Handle.ToInt64(), explorerPid = explorerPid,
            primaryBounds = Screen.PrimaryScreen.Bounds.ToString(), dpi = Native.GetDpiForWindow(form.Handle),
            noActivate = (Native.GetWindowLongPtr(form.Handle, -20).ToInt64() & 0x08000000L) != 0,
            iconViewVisible = Native.IsWindowVisible(icons), parentVerified = Native.GetParent(form.Handle) == parent };
    }

    internal void Detach(bool restoreWindow = false)
    {
        // Also undo partial attachment; all changes were limited to our own HWND.
        if (previousBounds.Width == 0 || !form.IsHandleCreated) return;
        IntPtr hwnd = form.Handle;
        form.Hide();
        Native.SetParent(hwnd, IntPtr.Zero);
        long style = Native.GetWindowLongPtr(hwnd, -16).ToInt64();
        Native.SetWindowLongPtr(hwnd, -16, new IntPtr(style & ~0x40000000L));
        long ex = Native.GetWindowLongPtr(hwnd, -20).ToInt64();
        Native.SetWindowLongPtr(hwnd, -20, new IntPtr(ex & ~(0x08000000L | 0x00080000L | 0x80L)));
        // Recovery/exit keeps the same HWND hidden. Changing WinForms border or
        // taskbar properties here recreates it and invalidates hotkey ownership.
        if (restoreWindow) {
            form.FormBorderStyle = FormBorderStyle.Sizable;
            form.ShowInTaskbar = true;
            form.Bounds = previousBounds;
            Native.SetWindowPos(form.Handle, IntPtr.Zero, 0, 0, 0, 0, 0x1 | 0x2 | 0x4 | 0x10 | 0x20);
            form.Show();
        }
        attached = false;
    }

    private static string ClassName(IntPtr window)
    {
        var text = new StringBuilder(128); Native.GetClassName(window, text, text.Capacity); return text.ToString();
    }

    internal static bool IsForeignFullscreen(IntPtr own, IntPtr chat)
    {
        IntPtr foreground = Native.GetForegroundWindow();
        if (foreground == IntPtr.Zero || foreground == own || foreground == chat) return false;
        string cls = ClassName(foreground);
        if (cls == "Progman" || cls == "WorkerW" || cls == "Shell_TrayWnd" || cls == "Shell_SecondaryTrayWnd") return false;
        uint pid; Native.GetWindowThreadProcessId(foreground, out pid);
        if (pid == System.Diagnostics.Process.GetCurrentProcess().Id || Native.IsIconic(foreground)) return false;
        Native.RECT r; if (!Native.GetWindowRect(foreground, out r)) return false;
        Rectangle screen = Screen.FromHandle(foreground).Bounds;
        return r.Left <= screen.Left && r.Top <= screen.Top && r.Right >= screen.Right && r.Bottom >= screen.Bottom;
    }

    internal static bool IsBlankDesktopPoint(int x, int y)
    {
        Native.POINT point = new Native.POINT { X = x, Y = y };
        IntPtr window = Native.WindowFromPoint(point);
        string cls = ClassName(Native.GetAncestor(window, 2));
        if (cls != "Progman" && cls != "WorkerW") return false;
        // MSAA hit testing preserves icons occupying the launch region. No remote memory or input injection.
        try {
            Accessibility.IAccessible accessible; object child;
            if (Native.AccessibleObjectFromPoint(point, out accessible, out child) != 0 || accessible == null) return false;
            int role = Convert.ToInt32(accessible.get_accRole(child));
            return role != 0x22 && role != 0x2a && role != 0x2b;
        } catch { return false; }
    }

    private static class Native
    {
        internal delegate bool EnumProc(IntPtr window, IntPtr state);
        [StructLayout(LayoutKind.Sequential)] internal struct POINT { internal int X, Y; }
        [StructLayout(LayoutKind.Sequential)] internal struct RECT { internal int Left, Top, Right, Bottom; }
        [DllImport("user32.dll")] internal static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] internal static extern IntPtr GetAncestor(IntPtr window, uint flags);
        [DllImport("user32.dll")] internal static extern IntPtr WindowFromPoint(POINT point);
        [DllImport("user32.dll")] internal static extern bool GetWindowRect(IntPtr window, out RECT rect);
        [DllImport("user32.dll")] internal static extern bool IsIconic(IntPtr window);
        [DllImport("oleacc.dll")] internal static extern int AccessibleObjectFromPoint(POINT point, [MarshalAs(UnmanagedType.Interface)] out Accessibility.IAccessible accessible, [MarshalAs(UnmanagedType.Struct)] out object child);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] internal static extern IntPtr FindWindow(string cls, string name);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] internal static extern IntPtr FindWindowEx(IntPtr parent, IntPtr after, string cls, string name);
        [DllImport("user32.dll")] internal static extern bool EnumWindows(EnumProc action, IntPtr state);
        [DllImport("user32.dll", SetLastError = true)] internal static extern IntPtr SetParent(IntPtr window, IntPtr parent);
        [DllImport("user32.dll")] internal static extern IntPtr GetParent(IntPtr window);
        [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")] internal static extern IntPtr GetWindowLongPtr(IntPtr window, int index);
        [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW", SetLastError = true)] internal static extern IntPtr SetWindowLongPtr(IntPtr window, int index, IntPtr value);
        [DllImport("user32.dll", SetLastError = true)] internal static extern bool SetWindowPos(IntPtr window, IntPtr after, int x, int y, int width, int height, uint flags);
        [DllImport("user32.dll", SetLastError = true)] internal static extern bool ScreenToClient(IntPtr window, ref POINT point);
        [DllImport("user32.dll", SetLastError = true)] internal static extern bool SetLayeredWindowAttributes(IntPtr window, uint color, byte alpha, uint flags);
        [DllImport("user32.dll")] internal static extern bool ShowWindow(IntPtr window, int command);
        [DllImport("user32.dll")] internal static extern bool IsWindow(IntPtr window);
        [DllImport("user32.dll")] internal static extern bool IsWindowVisible(IntPtr window);
        [DllImport("user32.dll")] internal static extern uint GetDpiForWindow(IntPtr window);
        [DllImport("user32.dll")] internal static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] internal static extern int GetClassName(IntPtr window, StringBuilder name, int count);
        [DllImport("user32.dll", SetLastError = true)] internal static extern IntPtr SendMessageTimeout(IntPtr window, uint message, IntPtr wparam, IntPtr lparam, uint flags, uint timeout, out UIntPtr result);
        [DllImport("user32.dll")] internal static extern IntPtr SendMessage(IntPtr window, uint message, IntPtr wparam, IntPtr lparam);
        [DllImport("kernel32.dll")] internal static extern void SetLastError(uint error);
    }
}
