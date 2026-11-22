// The admin hub's window: AgentArena.exe --admin on the admin computer (192.0.2.254). It shows the
// admin console (arena_service.admin_console, which answers this computer only) in its own WebView2
// window with the Arena icon and a tray icon, like the heads' viewer. It also keeps the event service
// running: when the service does not answer, it is started without a window (pythonw) and logs to its
// own file. The service is a separate process, so closing this window never stops it.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Management;
using System.Net;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace AgentArena
{
    public class AdminForm : Form
    {
        public const string MutexName = "Local\\AgentArenaAdminHub", ActivateEvent = "Local\\AgentArenaAdminHubActivate";
        const int ConsolePort = 8791;

        readonly string appDir, configPath, python, pythonw, serviceUrl, serviceLog;
        Process console;
        bool closing, consoleRestarted;
        WebView2 view;
        NotifyIcon tray;
        ToolStripMenuItem statusItem;
        readonly Button exitFull = new Button();
        bool fullscreen; Rectangle restoreBounds; FormWindowState restoreState;
        readonly System.Windows.Forms.Timer poll = new System.Windows.Forms.Timer { Interval = 10000 };

        public AdminForm(string appDir)
        {
            this.appDir = appDir;
            configPath = Path.Combine(Paths.LocalAppData, "AgentArenaService", "aes.config.json");
            var scripts = Path.Combine(appDir, ".venv", "Scripts");
            python = File.Exists(Path.Combine(scripts, "python.exe")) ? Path.Combine(scripts, "python.exe") : "python";
            pythonw = File.Exists(Path.Combine(scripts, "pythonw.exe")) ? Path.Combine(scripts, "pythonw.exe") : "pythonw";
            var cfg = Json.Parse(File.ReadAllText(configPath));
            serviceUrl = "http://" + (Json.Str(cfg, "host") ?? "127.0.0.1") + ":" + (Json.Str(cfg, "port") ?? "8790");
            serviceLog = Json.Str(cfg, "log_path");

            Text = Product.Name + " — Admin hub";
            Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
            MinimumSize = new Size(1100, 700); BackColor = Color.FromArgb(7, 19, 31); KeyPreview = true;
            var screen = Screen.PrimaryScreen.WorkingArea;
            StartPosition = FormStartPosition.Manual; Bounds = new Rectangle(screen.X + 30, screen.Y + 30, screen.Width - 60, screen.Height - 60);
            WindowState = FormWindowState.Maximized;
            exitFull.Text = "Exit full screen (Esc)"; exitFull.AutoSize = true; exitFull.Visible = false; exitFull.FlatStyle = FlatStyle.Flat;
            exitFull.BackColor = Color.FromArgb(25, 54, 74); exitFull.ForeColor = Color.White; exitFull.Anchor = AnchorStyles.Top | AnchorStyles.Right;
            exitFull.Click += delegate { SetFullscreen(false); };
            Controls.Add(exitFull);
            BuildTray();
            KeyDown += (s, e) => { if (e.KeyCode == Keys.F11) SetFullscreen(!fullscreen); else if (e.KeyCode == Keys.Escape && fullscreen) SetFullscreen(false); };
            Load += async delegate { await Task.Run(() => EnsureService()); UpdateStatus(); await StartConsole(); };
            FormClosing += delegate
            {
                closing = true; poll.Stop();
                Log.Info("admin window closing; the event service keeps running");
                StopConsole(); tray.Visible = false; tray.Dispose();
            };
            poll.Tick += delegate { UpdateStatus(); };
            poll.Start();
        }

        // ------------------------------------------------------------ window and tray
        public void SetFullscreen(bool on)
        {
            if (on == fullscreen) return; fullscreen = on;
            if (on) { restoreBounds = WindowState == FormWindowState.Normal ? Bounds : RestoreBounds; restoreState = WindowState; FormBorderStyle = FormBorderStyle.None; WindowState = FormWindowState.Normal; Bounds = Screen.FromControl(this).Bounds; }
            else { FormBorderStyle = FormBorderStyle.Sizable; Bounds = restoreBounds; WindowState = restoreState; }
            exitFull.Visible = on; exitFull.Location = new Point(ClientSize.Width - exitFull.Width - 16, 12); exitFull.BringToFront();
        }
        public void Activate2() { if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Maximized; Show(); Activate(); BringToFront(); }

        void BuildTray()
        {
            var menu = new ContextMenuStrip();
            menu.Items.Add("Show admin hub", null, delegate { Activate2(); });
            menu.Items.Add("Full screen (F11)", null, delegate { Activate2(); SetFullscreen(!fullscreen); });
            menu.Items.Add(new ToolStripSeparator());
            statusItem = new ToolStripMenuItem("Event service: checking…") { Enabled = false };
            menu.Items.Add(statusItem);
            menu.Items.Add("Restart the event service", null, delegate { RestartService(); });
            menu.Items.Add("Open the event service log", null, delegate
            {
                try { if (!string.IsNullOrEmpty(serviceLog) && File.Exists(serviceLog)) Process.Start("notepad.exe", "\"" + serviceLog + "\""); }
                catch (Exception ex) { Log.Error("open log: " + ex.Message); }
            });
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add("Exit admin window (the service keeps running)", null, delegate { Close(); });
            tray = new NotifyIcon { Icon = Icon, Text = "Agent Arena admin hub", ContextMenuStrip = menu, Visible = true };
            tray.DoubleClick += delegate { Activate2(); };
        }

        void UpdateStatus()
        {
            Task.Run(() => ServiceAnswers()).ContinueWith(t =>
            {
                if (closing) return;
                var up = t.Result;
                statusItem.Text = up ? "Event service: up (" + serviceUrl + ")" : "Event service: NOT answering";
                tray.Text = up ? "Agent Arena admin hub - service up" : "Agent Arena admin hub - service NOT answering";
            }, TaskScheduler.FromCurrentSynchronizationContext());
        }

        // ------------------------------------------------------------ the event service
        bool ServiceAnswers()
        {
            try
            {
                var req = (HttpWebRequest)WebRequest.Create(serviceUrl + "/v2/health"); req.Proxy = null; req.Timeout = 2500;
                using (var resp = (HttpWebResponse)req.GetResponse()) return resp.StatusCode == HttpStatusCode.OK;
            }
            catch { return false; }
        }

        // Python processes running one of this hub's modules with this hub's configuration. Matched on the
        // command line, not the executable: a venv's python.exe is a launcher whose child is the base Python.
        List<int> HubProcesses(string module)
        {
            var found = new List<int>();
            try
            {
                using (var q = new ManagementObjectSearcher("SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name='python.exe' OR Name='pythonw.exe'"))
                    foreach (ManagementObject p in q.Get())
                    {
                        var cmd = Convert.ToString(p["CommandLine"]) ?? "";
                        if (cmd.Contains("-m " + module + " ") && cmd.IndexOf(configPath, StringComparison.OrdinalIgnoreCase) >= 0) found.Add(Convert.ToInt32(p["ProcessId"]));
                    }
            }
            catch (Exception ex) { Log.Error("process list: " + ex.Message); }
            return found;
        }
        void Stop(string module)
        {
            foreach (var pid in HubProcesses(module)) try { var p = Process.GetProcessById(pid); p.Kill(); p.WaitForExit(5000); } catch { }
        }

        void EnsureService()
        {
            if (ServiceAnswers()) return;
            if (HubProcesses("arena_service").Count > 0) { Thread.Sleep(4000); if (ServiceAnswers()) return; }   // still starting
            StartService();
        }

        void StartService()
        {
            var psi = new ProcessStartInfo(pythonw, "-E -s -m arena_service --config \"" + configPath + "\"")
            { WorkingDirectory = appDir, UseShellExecute = false, CreateNoWindow = true };
            using (Process.Start(psi)) { }
            Log.Info("event service started without a window: " + pythonw);
            for (int i = 0; i < 20 && !ServiceAnswers(); i++) Thread.Sleep(500);
        }

        void RestartService()
        {
            if (MessageBox.Show(this, "Restart the Arena event service?\n\nDisplays reconnect by themselves within a few seconds.", Product.Name,
                MessageBoxButtons.OKCancel, MessageBoxIcon.Question) != DialogResult.OK) return;
            statusItem.Text = "Event service: restarting…";
            Task.Run(() =>
            {
                Stop("arena_service"); Log.Info("event service stopped for a restart");
                StartService();
            }).ContinueWith(delegate { UpdateStatus(); }, TaskScheduler.FromCurrentSynchronizationContext());
        }

        // ------------------------------------------------------------ the admin console
        async Task StartConsole()
        {
            Stop("arena_service.admin_console");     // left over from a window that did not close cleanly
            var psi = new ProcessStartInfo(python, "-E -s -u -m arena_service.admin_console --config \"" + configPath + "\" --port " + ConsolePort + " --no-browser")
            { WorkingDirectory = appDir, UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
            console = Process.Start(psi);
            console.EnableRaisingEvents = true;
            console.ErrorDataReceived += (o, e) => { if (!string.IsNullOrEmpty(e.Data)) Log.Info("console: " + e.Data); };
            console.BeginErrorReadLine();
            var url = await Task.Run(() =>
            {
                string line;
                while ((line = console.StandardOutput.ReadLine()) != null)
                    if (line.Contains("http://127.0.0.1:")) return line.Substring(line.IndexOf("http://", StringComparison.Ordinal)).Trim();
                return null;
            });
            if (url == null)
            {
                Log.Error("the admin console did not start (exit code " + (console.HasExited ? console.ExitCode.ToString() : "?") + ")");
                MessageBox.Show(this, "The admin console did not start. Details are in " + Log.FilePath + ".", Product.Name, MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }
            console.Exited += delegate
            {
                if (closing) return;
                Log.Error("the admin console stopped unexpectedly");
                if (!consoleRestarted) { consoleRestarted = true; BeginInvoke(new Action(async () => { await StartConsole(); })); }
            };
            var drain = Task.Run(() => { try { while (console.StandardOutput.ReadLine() != null) { } } catch { } });   // keep its output drained
            await ShowConsole(url);
        }

        void StopConsole()
        {
            try { if (console != null && !console.HasExited) { console.Kill(); console.WaitForExit(3000); } } catch { }
            Stop("arena_service.admin_console");     // the launcher's child (a venv's python.exe starts the base Python)
        }

        async Task ShowConsole(string url)
        {
            if (view == null)
            {
                view = new WebView2 { Dock = DockStyle.Fill, DefaultBackgroundColor = Color.FromArgb(7, 19, 31) };
                Controls.Add(view); exitFull.BringToFront();
                var env = await CoreWebView2Environment.CreateAsync(null, Path.Combine(Paths.Data, "admin-webview-data"));
                await view.EnsureCoreWebView2Async(env);
                var s = view.CoreWebView2.Settings;
                s.AreDevToolsEnabled = Environment.GetEnvironmentVariable("AGENT_ARENA_DEVTOOLS") == "1"; s.AreDefaultContextMenusEnabled = false;
                s.IsStatusBarEnabled = false; s.AreDefaultScriptDialogsEnabled = true;     // the console confirms retire, codes and addresses
                s.IsPasswordAutosaveEnabled = false; s.IsGeneralAutofillEnabled = false; s.AreHostObjectsAllowed = false;
                var origin = "http://127.0.0.1:" + ConsolePort + "/";
                view.CoreWebView2.NavigationStarting += (o, e) => { if (!e.Uri.StartsWith(origin)) { e.Cancel = true; Log.Info("blocked navigation to " + e.Uri); } };
                view.CoreWebView2.NewWindowRequested += (o, e) => { e.Handled = true; };
                view.CoreWebView2.WebMessageReceived += (o, e) =>        // View > Full screen, F11 and Esc from the page
                {
                    var m = e.WebMessageAsJson ?? "";
                    if (m.Contains("toggleFullscreen")) SetFullscreen(!fullscreen); else if (m.Contains("exitFullscreen")) SetFullscreen(false);
                };
                view.CoreWebView2.ProcessFailed += (o, e) =>
                {
                    Log.Error("WebView2 process failed: " + e.ProcessFailedKind + " " + e.Reason);
                    // A dead page or browser left the window blank until it was closed (2.1.2): rebuild it on the same page.
                    var k = e.ProcessFailedKind;
                    if (k != CoreWebView2ProcessFailedKind.BrowserProcessExited && k != CoreWebView2ProcessFailedKind.RenderProcessExited
                        && k != CoreWebView2ProcessFailedKind.RenderProcessUnresponsive) return;
                    var dead = view; var again = url;
                    BeginInvoke(new Action(async () =>
                    {
                        if (view != dead) return;
                        try { Controls.Remove(dead); dead.Dispose(); } catch { }
                        view = null; await Task.Delay(5000);
                        try { await ShowConsole(again); } catch (Exception ex) { Log.Error("admin display restart failed: " + ex.Message); }
                    }));
                };
            }
            view.CoreWebView2.Navigate(url);
            Log.Info("admin window showing the console");
        }
    }
}
