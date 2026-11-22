// Readable diagnostics that work with or without an installation, plus a redacted export.
using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Net.Sockets;
using System.Text;
using System.Windows.Forms;

namespace AgentArena
{
    public class Check { public string Status, Area, Result, Meaning; }

    public static class Diagnose
    {
        static Check C(string status, string area, string result, string meaning) { return new Check { Status = status, Area = area, Result = result, Meaning = meaning }; }

        public static List<Check> Run(DeploymentConfig deploy)
        {
            var list = new List<Check>();
            list.Add(C("INFO", "Application", "package " + Product.Version + "; running from " + Paths.ExeDir, ""));
            list.Add(C("INFO", "Computer", Environment.MachineName + " · user " + Environment.UserName + " · " + Environment.OSVersion.VersionString, ""));
            using (var g = Graphics.FromHwnd(IntPtr.Zero)) list.Add(C("INFO", "Display", string.Join("; ", Screen.AllScreens.Select(s => s.DeviceName + " " + s.Bounds.Width + "x" + s.Bounds.Height + (s.Primary ? " primary" : ""))) + " · scaling " + Math.Round(g.DpiX / 96f * 100) + "%", "Arena zoom: Ctrl+ / Ctrl- / Ctrl+0 or the tray icon menu"));
            foreach (var a in Identity.LocalAddresses()) list.Add(C("INFO", "Network", a.Item1 + " · " + a.Item3 + " · " + a.Item2, "read only; nothing is reconfigured"));
            var notes = new List<string>();
            var candidates = deploy == null ? new List<Candidate>() : Identity.Resolve(deploy, Identity.Endpoints(deploy), notes);
            if (candidates.Count == 1) list.Add(C("PASS", "Detected identity", candidates[0].DisplayId + " (" + candidates[0].Role + ") from " + candidates[0].Address, candidates[0].Reason));
            else if (candidates.Count > 1) list.Add(C("WARN", "Detected identity", string.Join(", ", candidates.Select(c => c.DisplayId + " (" + c.Role + ")")), "several identities match; the installer asks which one this computer is"));
            else list.Add(C("FAIL", "Detected identity", "none", "not a registered display and no address in an authorized display network"));
            foreach (var n in notes) list.Add(C("INFO", "Identity note", n, ""));

            string summary; var states = Engine.DetectState(out summary);
            list.Add(C(states.Contains("installed") ? "PASS" : states.Contains("not_installed") ? "FAIL" : "WARN", "Installation", summary.Replace(Environment.NewLine, " "),
                states.Contains("not_installed") ? "not installed: run INSTALL AGENT ARENA" : states.Contains("partial") ? "partially installed: run INSTALL again to repair" : ""));
            var dir = Paths.CurrentVersionDir();
            list.Add(C("INFO", "Install directory", dir ?? "(none)", "data: " + Paths.Data));
            LocalConfig cfg = null;
            try { cfg = LocalConfig.Load(); list.Add(C(cfg == null ? "WARN" : "PASS", "Configuration", cfg == null ? "config.json missing" : "valid · display " + cfg.DisplayId + " · role " + cfg.Role + " · location " + cfg.LocationId, cfg == null ? "misconfigured or not installed" : "")); }
            catch (Exception ex) { list.Add(C("FAIL", "Configuration", ex.Message, "misconfigured viewer: run INSTALL to repair")); }
            var endpoints = cfg != null ? cfg.Endpoints : deploy != null ? deploy.Endpoints : new List<string>();
            list.Add(C("INFO", "Event service address", string.Join(", ", endpoints), cfg != null ? "from installed configuration" : "from the package"));
            foreach (var ep in endpoints.Take(3))
            {
                var uri = new Uri(ep); bool tcp = false;
                try { using (var t = new TcpClient()) { var ar = t.BeginConnect(uri.Host, uri.Port, null, null); tcp = ar.AsyncWaitHandle.WaitOne(3000) && t.Connected; } } catch { }
                list.Add(C(tcp ? "PASS" : "FAIL", "Service reachability", uri.Host + ":" + uri.Port + (tcp ? " accepts connections" : " not reachable"), tcp ? "" : "coordinator / event service unavailable (not a local viewer fault)"));
                if (tcp)
                    try { var h = ArenaClient.Health(ep, 5); list.Add(C("PASS", "Arena event API", "service " + Json.Str(h, "service_id") + " · contracts " + string.Join(",", Json.List(h, "contracts")), "")); }
                    catch (Exception ex) { list.Add(C("FAIL", "Arena event API", ex.Message, "a port answers but it is not the Arena Event Service")); }
            }
            string id, secret;
            if (!CredentialStore.TryLoad(out id, out secret)) list.Add(C(cfg == null ? "INFO" : "FAIL", "Authentication", "no viewer credential for this Windows user", "login required: run INSTALL with an enrollment code"));
            else if (endpoints.Count > 0)
            {
                var client = new ArenaClient(endpoints[0], id, secret);
                try
                {
                    var conf = Json.Parse(client.Request("GET", "/v2/config", null, 10));
                    list.Add(C("PASS", "Authentication", "credential " + CredentialStore.Redact(id) + " accepted; service identity verified (signed response)", "display " + Json.Str(conf, "display_id") + " · " + Json.Str(conf, "location_id")));
                    var snap = Json.Parse(client.Request("GET", "/v2/snapshot?view=location:" + Json.Str(conf, "location_id"), null, 15));
                    object a; snap.TryGetValue("assignments", out a);
                    list.Add(C("PASS", "Viewer subscription", "snapshot at sequence " + Json.Str(snap, "as_of_seq") + " · " + ((System.Collections.ICollection)a).Count + " assignment(s) here now", "an idle head correctly shows none"));
                }
                catch (ArenaException ex) { list.Add(C("FAIL", "Authentication", ex.Code + ": " + ex.Message, ex.Code == "service_identity" ? "the service failed its authenticity check" : ex.Code == "refused" ? "this display's credential was revoked, replaced or is unknown to this service: re-enroll with a new code" : "login required / credential revoked: re-enroll")); }
                catch (Exception ex) { list.Add(C("WARN", "Authentication", "not checked: " + ex.Message, "service unreachable")); }
            }
            if (dir != null)
            {
                var problems = Engine.VerifyManifest(dir);
                list.Add(C(problems.Count == 0 ? "PASS" : "FAIL", "Local frontend integrity", problems.Count == 0 ? "all program and Arena web files match the manifest" : string.Join("; ", problems.Take(4)), problems.Count == 0 ? "" : "damaged: run INSTALL to repair"));
            }
            list.Add(C(Engine.ViewerRunning() ? "PASS" : "INFO", "Viewer process", Engine.ViewerRunning() ? "Agent Arena is running" : "not running", Engine.ViewerRunning() ? "" : "installed but not launched (or closed)"));
            var run = Startup.Current();
            list.Add(C(run == null ? "INFO" : (dir != null && run.IndexOf(dir, StringComparison.OrdinalIgnoreCase) >= 0 ? "PASS" : "WARN"), "Sign-in startup", run ?? "off", "one per-user Run entry; no service or scheduled task"));
            var legacy = Legacy.Inventory();
            list.Add(C(legacy.Count == 0 ? "PASS" : "WARN", "Original Phase 4.2 viewer", legacy.Count == 0 ? "none found" : string.Join("; ", legacy.Select(l => l.Kind + (l.Kind == "file" ? " " + Path.GetFileName(l.Detail) : ""))), legacy.Count == 0 ? "" : "run INSTALL with 'Retire the original viewer' to remove only its own items"));
            var wv = Prerequisites.WebView2Version();
            list.Add(C(wv == null ? "FAIL" : "PASS", "Display engine", wv == null ? "WebView2 Runtime missing" : "WebView2 Runtime " + wv, wv == null ? "missing browser engine: IT must install the WebView2 Runtime" : ""));
            list.Add(C("INFO", ".NET Framework", "release " + (Prerequisites.DotNetRelease() ?? "missing"), "528040 or later = 4.8"));
            var o = Telemetry.OllamaStatus();
            list.Add(C((bool)o["reachable"] ? "PASS" : "WARN", "Local Ollama", (bool)o["reachable"] ? "reachable · version " + Json.Str(o, "version") : "not reachable on 127.0.0.1:11434", "read-only probe; Arena never changes Ollama"));
            var smi = Telemetry.NvidiaSmi();
            list.Add(C("INFO", "Hardware telemetry", "CPU/RAM via Windows CIM; GPU " + (smi == null ? "unavailable (no nvidia-smi; normal on CPU-only heads)" : "via " + smi), cfg != null && cfg.Telemetry ? "reporting enabled" : "reporting disabled"));
            list.Add(C("INFO", "Persistent display", cfg != null && cfg.KeepAwake ? "on while the Arena window is open" : "off", "process-scoped request; released on exit; no power plan or lock policy is changed"));
            var errors = RecentErrors();
            list.Add(C(errors.Count == 0 ? "PASS" : "WARN", "Recent errors", errors.Count == 0 ? "none" : string.Join(" | ", errors), "log: " + Log.FilePath));
            return list;
        }

        static List<string> RecentErrors()
        {
            try { return File.ReadAllLines(Log.FilePath).Where(l => l.Contains(" ERROR ")).Reverse().Take(5).Select(l => l.Length > 160 ? l.Substring(0, 160) : l).ToList(); }
            catch { return new List<string>(); }
        }

        public static string Report(List<Check> checks)
        {
            var sb = new StringBuilder();
            sb.AppendLine("AGENT ARENA DIAGNOSTICS · " + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " · " + Environment.MachineName);
            sb.AppendLine("Secrets are never included; credential IDs are truncated.");
            foreach (var c in checks) sb.AppendLine(("[" + c.Status + "]").PadRight(7) + c.Area.PadRight(26) + c.Result + (string.IsNullOrEmpty(c.Meaning) ? "" : "  — " + c.Meaning));
            return sb.ToString();
        }
    }

    public class DiagnosticsForm : Form
    {
        readonly DeploymentConfig deploy; readonly ListView list = new ListView(); List<Check> checks = new List<Check>();
        public DiagnosticsForm(DeploymentConfig deploy)
        {
            this.deploy = deploy; Text = "ARENA VIEWER DIAGNOSTICS — " + Product.Name + " " + Product.Version; Size = new Size(1180, 720); StartPosition = FormStartPosition.CenterScreen;
            AutoScaleDimensions = new SizeF(96f, 96f); AutoScaleMode = AutoScaleMode.Dpi; Font = new Font("Segoe UI", 9.5f); Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
            list.View = View.Details; list.FullRowSelect = true; list.Dock = DockStyle.Fill; list.GridLines = true;
            var k = DeviceDpi / 96f;   // column widths follow the monitor's scaling
            list.Columns.Add("Status", (int)(70 * k)); list.Columns.Add("Check", (int)(190 * k)); list.Columns.Add("Result", (int)(560 * k)); list.Columns.Add("What it means", (int)(320 * k));
            var bar = new FlowLayoutPanel { Dock = DockStyle.Bottom, AutoSize = true, Padding = new Padding(8) };
            var rerun = new Button { Text = "Run again", AutoSize = true }; var export = new Button { Text = "Export report…", AutoSize = true };
            var copy = new Button { Text = "Copy report", AutoSize = true }; var close = new Button { Text = "Close", AutoSize = true };
            bar.Controls.AddRange(new Control[] { rerun, export, copy, close, new Label { Text = "Log: " + Log.FilePath, AutoSize = true, Padding = new Padding(12, 8, 0, 0) } });
            Controls.Add(list); Controls.Add(bar);
            rerun.Click += delegate { Populate(); }; close.Click += delegate { Close(); };
            copy.Click += delegate { Clipboard.SetText(Diagnose.Report(checks)); };
            export.Click += delegate
            {
                var path = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Desktop), "AgentArena-Diagnostics-" + Environment.MachineName + "-" + DateTime.Now.ToString("yyyyMMdd-HHmmss") + ".txt");
                File.WriteAllText(path, Diagnose.Report(checks)); MessageBox.Show(this, "Saved (no secrets included):\n" + path, Text);
            };
            Shown += delegate { Populate(); };
        }
        void Populate()
        {
            Cursor = Cursors.WaitCursor; list.Items.Clear();
            try { checks = Diagnose.Run(deploy); }
            catch (Exception ex) { checks = new List<Check> { new Check { Status = "FAIL", Area = "Diagnostics", Result = ex.Message, Meaning = "see " + Log.FilePath } }; Log.Error("diagnostics: " + ex); }
            foreach (var c in checks)
            {
                var item = new ListViewItem(new[] { c.Status, c.Area, c.Result, c.Meaning });
                item.BackColor = c.Status == "FAIL" ? Color.MistyRose : c.Status == "WARN" ? Color.LemonChiffon : c.Status == "PASS" ? Color.Honeydew : Color.White;
                list.Items.Add(item);
            }
            Cursor = Cursors.Default;
        }
    }
}
