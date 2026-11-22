// The Arena window: WebView2 hosting the locally installed frontend, plus the in-process
// bridge that forwards an allow-list of read-only routes to the shared event service.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Management;
using System.Net;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace AgentArena
{
    public class ViewerForm : Form
    {
        const string Host = "arena.local";
        static readonly string[] Allowed = { "/api/v2/snapshot", "/api/v2/events" };
        [DllImport("kernel32.dll")] static extern uint SetThreadExecutionState(uint flags);
        [StructLayout(LayoutKind.Sequential)]
        struct PerformanceInformation
        {
            public int Size; public UIntPtr CommitTotal, CommitLimit, CommitPeak, PhysicalTotal, PhysicalAvailable, SystemCache,
                KernelTotal, KernelPaged, KernelNonpaged, PageSize; public int HandleCount, ProcessCount, ThreadCount;
        }
        [DllImport("psapi.dll", SetLastError = true)] static extern bool GetPerformanceInfo(out PerformanceInformation info, int size);
        // Windows' kernel paged pool in bytes (no elevation needed), or -1.
        public static long KernelPagedBytes()
        {
            PerformanceInformation p;
            if (!GetPerformanceInfo(out p, Marshal.SizeOf(typeof(PerformanceInformation)))) return -1;
            return (long)p.KernelPaged.ToUInt64() * (long)p.PageSize.ToUInt64();
        }
        const uint ES_CONTINUOUS = 0x80000000, ES_SYSTEM_REQUIRED = 0x1, ES_DISPLAY_REQUIRED = 0x2;

        readonly LocalConfig config;
        readonly string webRoot;
        ArenaClient client;
        int endpointIndex;
        WebView2 view;
        readonly Button exitFull = new Button();
        bool fullscreen, startFullscreen; Rectangle restoreBounds; FormWindowState restoreState;
        System.Threading.Timer telemetry;
        CoreWebView2Environment env;
        int generation;                                   // which view a WebView2 event belongs to; stale events are ignored
        bool recycling;
        readonly Queue<DateTime> recycles = new Queue<DateTime>();
        readonly System.Windows.Forms.Timer guard = new System.Windows.Forms.Timer { Interval = 60000 };
        int overLimit; DateTime lastMemoryLog = DateTime.MinValue;
        // The display's WebView2 processes together may hold this much private memory before the view is rebuilt.
        // 2.1.2 and earlier had no limit: the page kept every finished task, and the heads ran out of commit memory after days.
        static readonly long LimitBytes = (long)EnvInt("AGENT_ARENA_WEBVIEW_LIMIT_MB", 1536) * 1048576;
        // Head 8 (2026-11-10, viewer 2.1.2): while the display was open Windows' kernel paged pool grew about 1.1 GB an hour
        // (graphics memory manager allocations, tag Vi54 in dxgmms2.sys) and fell back by 19 GB when the display closed.
        // That memory is not in any process's private bytes, so the limit above never saw it. If the kernel paged pool
        // grows this much while one display is open, the display is rebuilt, which returns it.
        static readonly long KernelGrowthBytes = (long)EnvInt("AGENT_ARENA_KERNEL_GROWTH_MB", 2048) * 1048576;
        long kernelBaseline = -1; int kernelOver;
        static int EnvInt(string name, int fallback) { int v; return int.TryParse(Environment.GetEnvironmentVariable(name), out v) && v > 0 ? v : fallback; }
        double zoom = 1.0;
        NotifyIcon tray;

        public ViewerForm(LocalConfig config, string webRoot)
        {
            this.config = config; this.webRoot = webRoot;
            Text = Product.Name + " — " + (config.DisplayId ?? "not enrolled");
            Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
            MinimumSize = new Size(960, 600); BackColor = Color.FromArgb(13, 28, 41); KeyPreview = true;
            RestoreWindow();
            exitFull.Text = "Exit full screen (Esc)"; exitFull.AutoSize = true; exitFull.Visible = false; exitFull.FlatStyle = FlatStyle.Flat;
            exitFull.BackColor = Color.FromArgb(25, 54, 74); exitFull.ForeColor = Color.White; exitFull.Anchor = AnchorStyles.Top | AnchorStyles.Right;
            exitFull.Click += delegate { SetFullscreen(false); };
            Controls.Add(exitFull);
            BuildTray();
            string id, secret;
            if (CredentialStore.TryLoad(out id, out secret) && config.Endpoints.Count > 0) client = new ArenaClient(config.Endpoints[0], id, secret);
            KeyDown += (s, e) => { if (e.KeyCode == Keys.F11) SetFullscreen(!fullscreen); else if (e.KeyCode == Keys.Escape && fullscreen) SetFullscreen(false); };
            Load += async delegate { await StartWebView(); };
            Shown += delegate { if (startFullscreen) SetFullscreen(true); };
            FormClosing += (s, e) =>
            {
                // Field defect D6: every close records why (window button/Alt+F4 = UserClosing, Windows shutdown, Task Manager, …).
                Log.Info("viewer closing: reason " + e.CloseReason + ", source " + (closeSource ?? "window or system (" + e.CloseReason + ")"));
                SaveWindow(); SetThreadExecutionState(ES_CONTINUOUS); if (telemetry != null) telemetry.Dispose(); guard.Stop(); tray.Visible = false; tray.Dispose();
            };
            guard.Tick += delegate { CheckMemory(); };
            Resize += delegate { SetMemoryTarget(); };
            if (config.KeepAwake) SetThreadExecutionState(ES_CONTINUOUS | ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED);
            if (config.Telemetry && config.Role == "head") telemetry = new System.Threading.Timer(_ => SendTelemetry(), null, 5000, 30000);
        }

        // ------------------------------------------------------------ window
        void RestoreWindow()
        {
            StartPosition = FormStartPosition.Manual; var screen = Screen.PrimaryScreen.WorkingArea;
            Bounds = new Rectangle(screen.X + 40, screen.Y + 40, Math.Min(1600, screen.Width - 80), Math.Min(960, screen.Height - 80));
            try
            {
                var d = Json.Parse(File.ReadAllText(Paths.WindowFile));
                var r = new Rectangle(int.Parse(Json.Str(d, "x")), int.Parse(Json.Str(d, "y")), int.Parse(Json.Str(d, "w")), int.Parse(Json.Str(d, "h")));
                if (Screen.AllScreens.Any(s => s.WorkingArea.IntersectsWith(r)) && r.Width >= 600) Bounds = r;
                if (Json.Str(d, "maximized") == "True") WindowState = FormWindowState.Maximized;
                // A display kept full screen (a dedicated monitor, e.g. the Capstone laptop's second screen) comes back full screen.
                startFullscreen = Json.Str(d, "fullscreen") == "True";
                double z; if (double.TryParse(Json.Str(d, "zoom"), System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out z) && z >= 0.5 && z <= 2.5) zoom = z;
            }
            catch { }
        }
        void SaveWindow()
        {
            try
            {
                var b = fullscreen ? restoreBounds : (WindowState == FormWindowState.Normal ? Bounds : RestoreBounds);
                File.WriteAllText(Paths.WindowFile, Json.Write(new Dictionary<string, object> { { "x", b.X }, { "y", b.Y }, { "w", b.Width }, { "h", b.Height },
                    { "maximized", (fullscreen ? restoreState : WindowState) == FormWindowState.Maximized }, { "fullscreen", fullscreen }, { "zoom", zoom.ToString(System.Globalization.CultureInfo.InvariantCulture) } }));
            }
            catch { }
        }
        public void SetFullscreen(bool on)
        {
            if (on == fullscreen) return; fullscreen = on;
            if (on) { restoreBounds = WindowState == FormWindowState.Normal ? Bounds : RestoreBounds; restoreState = WindowState; FormBorderStyle = FormBorderStyle.None; WindowState = FormWindowState.Normal; Bounds = Screen.FromControl(this).Bounds; }
            else { FormBorderStyle = FormBorderStyle.Sizable; Bounds = restoreBounds; WindowState = restoreState; }
            exitFull.Visible = on; exitFull.Location = new Point(ClientSize.Width - exitFull.Width - 16, 12); exitFull.BringToFront();
        }
        string closeSource;
        public void CloseFor(string source) { closeSource = source; Close(); }
        public void Activate2() { if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal; Show(); Activate(); BringToFront(); }

        // Notification-area icon: a visible sign Arena is running, and quick controls.
        void BuildTray()
        {
            var menu = new ContextMenuStrip();
            menu.Items.Add("Show Agent Arena", null, delegate { Activate2(); });
            menu.Items.Add("Full screen (F11)", null, delegate { Activate2(); SetFullscreen(!fullscreen); });
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add("Zoom in (Ctrl +)", null, delegate { SetZoom(zoom + 0.1); });
            menu.Items.Add("Zoom out (Ctrl -)", null, delegate { SetZoom(zoom - 0.1); });
            menu.Items.Add("Reset zoom (Ctrl 0)", null, delegate { SetZoom(1.0); });
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add("Diagnostics", null, delegate { try { Process.Start(Application.ExecutablePath, "--diagnostics"); } catch (Exception ex) { Log.Error("diagnostics: " + ex.Message); } });
            menu.Items.Add("Exit Agent Arena", null, delegate { CloseFor("tray menu Exit"); });
            tray = new NotifyIcon { Icon = Icon, Text = ("Agent Arena - " + (config.DisplayId ?? "not enrolled")).Substring(0, Math.Min(63, ("Agent Arena - " + (config.DisplayId ?? "not enrolled")).Length)), ContextMenuStrip = menu, Visible = true };
            tray.DoubleClick += delegate { Activate2(); };
        }
        void SetZoom(double z)
        {
            zoom = Math.Max(0.5, Math.Min(2.5, Math.Round(z, 2)));
            if (view != null && view.CoreWebView2 != null) view.ZoomFactor = zoom;
            SaveWindow();
        }

        // ------------------------------------------------------------ webview
        async Task StartWebView()
        {
            var gen = ++generation;
            view = new WebView2 { Dock = DockStyle.Fill, DefaultBackgroundColor = Color.FromArgb(13, 28, 41) };
            Controls.Add(view); exitFull.BringToFront();
            env = await CoreWebView2Environment.CreateAsync(null, Paths.WebViewData);
            await view.EnsureCoreWebView2Async(env);
            if (gen != generation) return;
            recycling = false; overLimit = 0; kernelOver = 0; kernelBaseline = -1; SetMemoryTarget(); guard.Start();
            view.ZoomFactor = zoom;                                   // per-machine Arena zoom, independent of Windows scaling
            view.ZoomFactorChanged += delegate { zoom = view.ZoomFactor; };
            var core = view.CoreWebView2; var s = core.Settings;
            s.AreDevToolsEnabled = Environment.GetEnvironmentVariable("AGENT_ARENA_DEVTOOLS") == "1"; s.AreDefaultContextMenusEnabled = false;
            s.IsStatusBarEnabled = false; s.AreDefaultScriptDialogsEnabled = false; s.IsWebMessageEnabled = true; s.AreHostObjectsAllowed = false;
            s.IsPasswordAutosaveEnabled = false; s.IsGeneralAutofillEnabled = false;
            // Every arena.local request is answered in-process: verified local files, or the read-only bridge.
            core.AddWebResourceRequestedFilter("https://" + Host + "/*", CoreWebView2WebResourceContext.All);
            core.WebResourceRequested += OnApiRequest;
            core.NavigationStarting += (o, e) => { if (!e.Uri.StartsWith("https://" + Host + "/")) { e.Cancel = true; Log.Info("blocked navigation to " + e.Uri); } };
            core.NewWindowRequested += (o, e) => { e.Handled = true; };
            core.WebMessageReceived += (o, e) =>
            {
                var m = e.WebMessageAsJson ?? "";
                if (m.Contains("toggleFullscreen")) SetFullscreen(!fullscreen); else if (m.Contains("exitFullscreen")) SetFullscreen(false);
            };
            core.ProcessFailed += (o, e) =>
            {
                if (gen != generation) return;
                var kind = e.ProcessFailedKind;
                Log.Error("WebView2 process failed: " + kind + " " + e.Reason + " (exit " + e.ExitCode + ")");
                // GPU, utility and helper processes are restarted by WebView2 itself; rebuilding the view for them
                // (2.1.2) only added churn. The page, its renderer or the browser itself gone: rebuild once.
                if (kind == CoreWebView2ProcessFailedKind.BrowserProcessExited || kind == CoreWebView2ProcessFailedKind.RenderProcessExited
                    || kind == CoreWebView2ProcessFailedKind.RenderProcessUnresponsive)
                    BeginInvoke(new Action(() => Recycle(kind.ToString())));
                else if (kind == CoreWebView2ProcessFailedKind.FrameRenderProcessExited)
                    BeginInvoke(new Action(() => { try { if (gen == generation) view.CoreWebView2.Reload(); } catch (Exception ex) { Log.Error("reload after frame failure: " + ex.Message); } }));
            };
            core.Navigate("https://" + Host + "/agent_arena_desktop.html");
        }

        // Rebuild the view from nothing: the old browser processes exit and their memory returns to Windows.
        // Never a modal box (2.1.2 stopped restarting after five failures and waited on a dialog nobody saw).
        void Recycle(string why)
        {
            if (recycling || IsDisposed) return;
            var now = DateTime.UtcNow; while (recycles.Count > 0 && now - recycles.Peek() > TimeSpan.FromMinutes(10)) recycles.Dequeue();
            recycling = true; guard.Stop(); generation++;
            var delay = recycles.Count >= 5 ? 300000 : 5000;   // more than five in ten minutes: back off for five minutes
            recycles.Enqueue(now);
            Log.Info("rebuilding the display: " + why + (delay > 5000 ? " (repeated failures; waiting 5 minutes first)" : ""));
            var old = view; view = null;
            try { if (old != null) { Controls.Remove(old); old.Dispose(); } } catch (Exception ex) { Log.Error("disposing the old display: " + ex.Message); }
            var wait = new System.Windows.Forms.Timer { Interval = delay };
            wait.Tick += async delegate
            {
                wait.Stop(); wait.Dispose();
                try { await StartWebView(); } catch (Exception ex) { Log.Error("display restart failed: " + ex.Message); recycling = false; Recycle("restart failed"); }
            };
            wait.Start();
        }

        void SetMemoryTarget()
        {
            try
            {
                if (view == null || view.CoreWebView2 == null) return;
                view.CoreWebView2.MemoryUsageTargetLevel = WindowState == FormWindowState.Minimized ? CoreWebView2MemoryUsageTargetLevel.Low : CoreWebView2MemoryUsageTargetLevel.Normal;
            }
            catch (Exception ex) { Log.Write("WARN", "memory target: " + ex.Message); }
        }

        // Private memory of every WebView2 process serving this display, by kind; the app's own in "app".
        public Dictionary<string, long> MemoryByKind()
        {
            var d = new Dictionary<string, long>();
            using (var self = Process.GetCurrentProcess()) d["app"] = self.PrivateMemorySize64;
            if (env == null) return d;
            foreach (var info in env.GetProcessInfos())
                try
                {
                    using (var p = Process.GetProcessById(info.ProcessId))
                    {
                        var k = info.Kind.ToString(); long v; d.TryGetValue(k, out v); d[k] = v + p.PrivateMemorySize64;
                    }
                }
                catch { }   // exited between the list and the read
            return d;
        }

        void CheckMemory()
        {
            if (recycling || view == null || view.CoreWebView2 == null) return;
            Dictionary<string, long> m;
            try { m = MemoryByKind(); } catch (Exception ex) { Log.Write("WARN", "memory check: " + ex.Message); return; }
            long web = m.Where(kv => kv.Key != "app").Sum(kv => kv.Value);
            // The first check after a (re)start, a minute in, sets the baseline: by then the old display's memory is back.
            long kernel = KernelPagedBytes();
            if (kernel >= 0 && kernelBaseline < 0) { kernelBaseline = kernel; Log.Info("kernel paged pool " + kernel / 1048576 + " MB with the display open"); }
            long grown = kernel >= 0 && kernelBaseline >= 0 ? kernel - kernelBaseline : 0;
            if (DateTime.UtcNow - lastMemoryLog > TimeSpan.FromMinutes(30))
            {
                lastMemoryLog = DateTime.UtcNow;
                Log.Info("memory: display " + web / 1048576 + " MB (" + string.Join(", ", m.Where(kv => kv.Key != "app").Select(kv => kv.Key + " " + kv.Value / 1048576)) + "), app " + m["app"] / 1048576 + " MB, limit " + LimitBytes / 1048576
                    + " MB; kernel paged pool " + (kernel >= 0 ? (kernel / 1048576).ToString() : "?") + " MB (" + (grown >= 0 ? "+" : "") + grown / 1048576 + " MB since the display started, limit +" + KernelGrowthBytes / 1048576 + " MB)");
            }
            overLimit = web > LimitBytes ? overLimit + 1 : 0;
            kernelOver = grown > KernelGrowthBytes ? kernelOver + 1 : 0;
            if (overLimit >= 2) Recycle("display memory " + web / 1048576 + " MB over the " + LimitBytes / 1048576 + " MB limit");
            else if (kernelOver >= 2) Recycle("kernel paged pool " + kernel / 1048576 + " MB, +" + grown / 1048576 + " MB since the display started (limit +" + KernelGrowthBytes / 1048576 + " MB)");
        }

        void OnApiRequest(object sender, CoreWebView2WebResourceRequestedEventArgs e)
        {
            var uri = new Uri(e.Request.Uri); var method = e.Request.Method;
            if (!uri.AbsolutePath.StartsWith("/api/"))
            {
                if (Environment.GetEnvironmentVariable("AGENT_ARENA_TRACE") == "1" && !uri.AbsolutePath.StartsWith("/assets/")) Log.Info("page request " + uri.PathAndQuery);
                e.Response = StaticFile(((CoreWebView2)sender).Environment, uri.AbsolutePath); return;
            }
            if (Environment.GetEnvironmentVariable("AGENT_ARENA_TRACE") == "1") Log.Info("bridge request " + method + " " + uri.PathAndQuery);
            var deferral = e.GetDeferral(); var env = ((CoreWebView2)sender).Environment;   // this view's, even if a rebuild replaces it meanwhile
            Task.Run(() =>
            {
                int status; string body;
                try { body = Bridge(method, uri, out status); }
                catch (Exception ex) { status = 500; body = Error("bridge_error", ex.Message, "bridge"); Log.Error("bridge " + uri.AbsolutePath + ": " + ex); }
                BeginInvoke(new Action(() =>
                {
                    try
                    {
                        var bytes = new MemoryStream(Encoding.UTF8.GetBytes(body));
                        e.Response = env.CreateWebResourceResponse(bytes, status, status == 200 ? "OK" : "Error", "Content-Type: application/json\r\nCache-Control: no-store");
                        if (Environment.GetEnvironmentVariable("AGENT_ARENA_TRACE") == "1") Log.Info("bridge response " + status + " " + uri.AbsolutePath);
                    }
                    catch (Exception ex) { Log.Error("bridge response failed: " + ex);
                    }
                    finally { try { deferral.Complete(); } catch (Exception ex) { Log.Write("WARN", "request finished after its display was rebuilt: " + ex.Message); } }
                }));
            });
        }

        static readonly Dictionary<string, string> Mime = new Dictionary<string, string> { { ".html", "text/html; charset=utf-8" }, { ".css", "text/css; charset=utf-8" },
            { ".js", "text/javascript; charset=utf-8" }, { ".mjs", "text/javascript; charset=utf-8" }, { ".png", "image/png" }, { ".svg", "image/svg+xml" },
            { ".json", "application/json; charset=utf-8" } };
        CoreWebView2WebResourceResponse StaticFile(CoreWebView2Environment env, string path)
        {
            var rel = Uri.UnescapeDataString(path).TrimStart('/').Replace('/', '\\');
            var full = Path.GetFullPath(Path.Combine(webRoot, rel)); string type;
            if (!full.StartsWith(Path.GetFullPath(webRoot) + "\\", StringComparison.OrdinalIgnoreCase) || !File.Exists(full) || !Mime.TryGetValue(Path.GetExtension(full).ToLowerInvariant(), out type))
                return env.CreateWebResourceResponse(new MemoryStream(Encoding.UTF8.GetBytes("not found")), 404, "Not Found", "Content-Type: text/plain");
            return env.CreateWebResourceResponse(new MemoryStream(File.ReadAllBytes(full)), 200, "OK", "Content-Type: " + type + "\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff");
        }

        static string Error(string code, string message, string origin)
        {
            return Json.Write(new Dictionary<string, object> { { "error", code }, { "message", message }, { "origin", origin } });
        }

        string Bridge(string method, Uri uri, out int status)
        {
            status = 200;
            if (method != "GET") { status = 405; return Error("not_allowed", "Read-only bridge", "bridge"); }
            if (client == null) { status = 403; return Error("not_enrolled", "This display has no viewer credential", "bridge"); }
            if (uri.AbsolutePath == "/api/local/identity")
            {
                var cfg = Json.Parse(Call("/v2/config", 10, out status));
                if (status != 200) return Json.Write(cfg);
                ApplyRotation(cfg);
                return Json.Write(new Dictionary<string, object> { { "display_id", Json.Str(cfg, "display_id") }, { "role", Json.Str(cfg, "role") },
                    { "location_id", Json.Str(cfg, "location_id") }, { "service", new Dictionary<string, object> { { "url", client.BaseUrl }, { "service_id", Json.Str(cfg, "service_id") } } },
                    { "version", Product.Version } });
            }
            if (!Allowed.Contains(uri.AbsolutePath)) { status = 404; return Error("not_allowed", "Not a permitted bridge route", "bridge"); }
            return Call("/v2" + uri.PathAndQuery.Substring("/api/v2".Length), 35, out status);
        }

        string Call(string target, int timeout, out int status)
        {
            // Try the configured endpoint, then any others published by the service.
            for (int attempt = 0; attempt < Math.Max(1, config.Endpoints.Count); attempt++)
            {
                try { status = 200; return client.Request("GET", target, null, timeout); }
                catch (ArenaException ex)
                {
                    status = ex.Status;
                    var code = ex.Status == 403 ? "not_authorized" : ex.Status == 410 ? "resnapshot" : ex.Code;
                    return Error(code, ex.Message, ex.Code == "service_identity" ? "bridge" : "service");
                }
                catch (WebException)
                {
                    if (config.Endpoints.Count < 2) break;
                    endpointIndex = (endpointIndex + 1) % config.Endpoints.Count;
                    client = new ArenaClient(config.Endpoints[endpointIndex], client.CredentialId, SecretFor());
                }
            }
            status = 503; return Error("service_unreachable", client.BaseUrl, "bridge");
        }
        string SecretFor() { string id, secret; CredentialStore.TryLoad(out id, out secret); return secret; }

        void ApplyRotation(Dictionary<string, object> cfg)
        {
            // Endpoint migration and credential rotation arrive signed through /v2/config.
            var endpoints = Json.List(cfg, "endpoints").Where(u => u.StartsWith("http://") || u.StartsWith("https://")).ToList();
            if (endpoints.Count > 0 && !endpoints.SequenceEqual(config.Endpoints))
            {
                var candidate = endpoints[0];
                try
                {
                    var probe = new ArenaClient(candidate, client.CredentialId, SecretFor());
                    probe.Request("GET", "/v2/config", null, 10);        // must answer with a valid signature first
                    config.Endpoints = endpoints; config.Save(); client = probe; endpointIndex = 0;
                    Log.Info("event service endpoints updated to " + string.Join(", ", endpoints));
                }
                catch (Exception ex) { Log.Error("published endpoint " + candidate + " failed validation: " + ex.Message); }
            }
            object rot; cfg.TryGetValue("rotation", out rot);
            var r = rot as Dictionary<string, object>;
            if (r != null)
            {
                var newId = Json.Str(r, "credential_id"); var secret = Encoding.UTF8.GetString(client.Unseal(Json.Str(r, "sealed_secret")));
                var next = new ArenaClient(client.BaseUrl, newId, secret);
                next.Request("GET", "/v2/config", null, 10);
                CredentialStore.Save(newId, secret); client = next;
                Log.Info("viewer credential rotated to " + CredentialStore.Redact(newId));
            }
        }

        // ------------------------------------------------------------ telemetry
        void SendTelemetry()
        {
            if (client == null) return;
            try { client.Request("POST", "/v2/telemetry", Json.Write(Telemetry.Sample()), 10); }
            catch (Exception ex) { Log.Write("WARN", "telemetry not delivered: " + ex.Message); }
        }
    }

    public static class Telemetry
    {
        public static Dictionary<string, object> Sample()
        {
            var validity = new Dictionary<string, object>(); var d = new Dictionary<string, object>();
            d["sampled_at"] = (DateTime.UtcNow - new DateTime(1970, 1, 1)).TotalSeconds;
            try
            {
                double total = 0; int n = 0;
                // Every WMI object is released here: this runs every 30 seconds for as long as the display is up.
                using (var s = new ManagementObjectSearcher("SELECT LoadPercentage FROM Win32_Processor")) using (var rows = s.Get())
                    foreach (ManagementObject o in rows) using (o) if (o["LoadPercentage"] != null) { total += Convert.ToDouble(o["LoadPercentage"]); n++; }
                d["cpu_percent"] = n > 0 ? (object)(total / n) : null; validity["cpu"] = n > 0 ? "measured" : "unavailable:no_load_counter";
                using (var s = new ManagementObjectSearcher("SELECT TotalVisibleMemorySize, FreePhysicalMemory FROM Win32_OperatingSystem")) using (var rows = s.Get())
                    foreach (ManagementObject o in rows) using (o) { d["ram_total_bytes"] = Convert.ToInt64(o["TotalVisibleMemorySize"]) * 1024; d["ram_available_bytes"] = Convert.ToInt64(o["FreePhysicalMemory"]) * 1024; }
                validity["ram"] = "measured";
            }
            catch (Exception ex) { validity["cpu"] = "unavailable:" + ex.GetType().Name; }
            var gpus = new List<object>(); var smi = NvidiaSmi();
            if (smi == null) validity["gpu"] = "unavailable:no_nvidia_smi";
            else
                try
                {
                    var psi = new ProcessStartInfo(smi, "--query-gpu=index,name,utilization.gpu,memory.total,memory.free --format=csv,noheader,nounits") { UseShellExecute = false, RedirectStandardOutput = true, CreateNoWindow = true };
                    using (var p = Process.Start(psi))
                    {
                        var output = p.StandardOutput.ReadToEnd(); if (!p.WaitForExit(5000)) { p.Kill(); throw new TimeoutException("nvidia-smi timeout"); }
                        foreach (var line in output.Split(new[] { '\n' }, StringSplitOptions.RemoveEmptyEntries))
                        {
                            var f = line.Split(',').Select(x => x.Trim()).ToArray(); if (f.Length < 5) continue;
                            gpus.Add(new Dictionary<string, object> { { "index", int.Parse(f[0]) }, { "name", f[1].Length > 60 ? f[1].Substring(0, 60) : f[1] },
                                { "util_percent", double.Parse(f[2], System.Globalization.CultureInfo.InvariantCulture) },
                                { "vram_total_bytes", long.Parse(f[3]) * 1048576 }, { "vram_available_bytes", long.Parse(f[4]) * 1048576 } });
                        }
                    }
                    validity["gpu"] = gpus.Count > 0 ? "measured" : "unavailable:no_gpu_reported";
                }
                catch (Exception ex) { validity["gpu"] = "unavailable:" + ex.GetType().Name; }
            d["gpus"] = gpus;
            d["ollama"] = OllamaStatus();
            d["validity"] = validity;
            return d;
        }
        public static string NvidiaSmi()
        {
            var candidates = new[] { Path.Combine(Environment.SystemDirectory, "nvidia-smi.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), @"NVIDIA Corporation\NVSMI\nvidia-smi.exe") };
            return candidates.FirstOrDefault(File.Exists);
        }
        public static Dictionary<string, object> OllamaStatus()
        {
            // Read-only probe of the local inference service; never starts, stops or configures it.
            try
            {
                var req = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:11434/api/version"); req.Timeout = 2000; req.Proxy = null;
                using (var r = (HttpWebResponse)req.GetResponse()) using (var sr = new StreamReader(r.GetResponseStream()))
                    return new Dictionary<string, object> { { "reachable", true }, { "version", Json.Str(Json.Parse(sr.ReadToEnd()), "version") } };
            }
            catch { return new Dictionary<string, object> { { "reachable", false } }; }
        }
    }
}
