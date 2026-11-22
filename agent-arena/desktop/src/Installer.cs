// Install / repair / upgrade / uninstall engine and the installer window.
// Per-user, no administrator rights, no services, no power/firewall/network changes.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Threading;
using System.Windows.Forms;

namespace AgentArena
{
    public class InstallOptions
    {
        public Candidate Identity; public string EnrollmentCode; public bool StartAtSignIn = true, KeepAwake = true, Telemetry = true;
        public bool RetireLegacy = true, DeleteLegacyBrowserData, DesktopShortcut = true;
    }

    public static class Engine
    {
        public static string PackageAppDir { get { return Paths.ExeDir; } }
        public static string ResultFile { get { return Path.Combine(Paths.Data, "last-setup-result.json"); } }

        /// <summary>Lets a waiting script continue without the setup window having to be closed.</summary>
        public static void WriteResult(string action, bool ok)
        {
            try
            {
                Directory.CreateDirectory(Paths.Data); string id, secret;
                File.WriteAllText(ResultFile, Json.Write(new Dictionary<string, object> { { "action", action }, { "ok", ok }, { "version", Product.Version },
                    { "finished_at", DateTime.UtcNow.ToString("o") }, { "enrolled", CredentialStore.TryLoad(out id, out secret) } }));
            }
            catch { }
        }

        public static Dictionary<string, string> Manifest(string dir)
        {
            var d = Json.Parse(File.ReadAllText(Path.Combine(dir, "app-manifest.json")));
            object files; d.TryGetValue("files", out files);
            return ((Dictionary<string, object>)files).ToDictionary(kv => kv.Key, kv => Convert.ToString(kv.Value));
        }
        public static List<string> VerifyManifest(string dir)
        {
            var problems = new List<string>();
            Dictionary<string, string> m;
            try { m = Manifest(dir); } catch (Exception ex) { problems.Add("app-manifest.json unreadable: " + ex.Message); return problems; }
            using (var sha = SHA256.Create())
                foreach (var kv in m)
                {
                    var f = Path.Combine(dir, kv.Key.Replace('/', '\\'));
                    if (!File.Exists(f)) { problems.Add("missing " + kv.Key); continue; }
                    string h; using (var s = File.OpenRead(f)) h = BitConverter.ToString(sha.ComputeHash(s)).Replace("-", "");
                    if (!h.Equals(kv.Value, StringComparison.OrdinalIgnoreCase)) problems.Add("modified " + kv.Key);
                }
            return problems;
        }

        /// <summary>Installation-state detector shared by installer and diagnostics.</summary>
        public static List<string> DetectState(out string summary)
        {
            var states = new List<string>(); var notes = new List<string>();
            var current = Paths.CurrentVersionDir();
            var versionDirs = Directory.Exists(Paths.Install) ? Directory.GetDirectories(Paths.Install).Select(Path.GetFileName).ToList() : new List<string>();
            if (versionDirs.Any(v => v.EndsWith(".partial"))) { states.Add("partial"); notes.Add("an interrupted install left " + string.Join(", ", versionDirs.Where(v => v.EndsWith(".partial")))); }
            if (current == null && versionDirs.Any(v => !v.EndsWith(".partial") && !v.EndsWith(".old"))) { states.Add("partial"); notes.Add("program files present but no current version pointer"); }
            LocalConfig cfg = null;
            try { cfg = LocalConfig.Load(); } catch (Exception ex) { states.Add("misconfigured"); notes.Add("config.json: " + ex.Message); }
            if (current != null)
            {
                var problems = VerifyManifest(current);
                if (problems.Count > 0) { states.Add("damaged"); notes.Add(string.Join("; ", problems.Take(5))); }
                var installed = Path.GetFileName(current);
                if (new Version(installed) < new Version(Product.Version)) { states.Add("upgrade_available"); notes.Add("installed " + installed + ", package " + Product.Version); }
                if (cfg == null && !states.Contains("misconfigured")) { states.Add("partial"); notes.Add("program installed but config.json missing"); }
                string id, secret;
                if (cfg != null && !CredentialStore.TryLoad(out id, out secret)) { states.Add("installed_not_enrolled"); notes.Add("no viewer credential stored for this Windows user"); }
                var run = Startup.Current();
                if (cfg != null && cfg.StartAtSignIn && (run == null || run.IndexOf(current, StringComparison.OrdinalIgnoreCase) < 0)) { states.Add("misconfigured"); notes.Add("sign-in startup does not point at the current version"); }
                if (states.Count == 0) states.Add("installed");
            }
            else if (!states.Contains("partial")) states.Add("not_installed");
            var legacy = Legacy.Inventory();
            var active = legacy.Where(l => l.Kind != "browser-profile" && (l.Owned || l.Kind != "file")).ToList();
            if (active.Count > 0) { states.Add("legacy_phase42_viewer_present"); notes.Add("original Phase 4.2 viewer: " + string.Join(", ", active.Select(l => l.Kind).Distinct())); }
            else if (legacy.Count > 0) { states.Add("legacy_viewer_data_retained"); notes.Add("kept from the original viewer (not running, no startup): " + string.Join(", ", legacy.Select(l => l.Kind == "file" ? Path.GetFileName(l.Detail) : l.Kind))); }
            summary = string.Join(", ", states.Distinct()) + (notes.Count > 0 ? Environment.NewLine + "  - " + string.Join(Environment.NewLine + "  - ", notes) : "");
            return states.Distinct().ToList();
        }

        public static bool ViewerRunning()
        {
            bool created; using (var m = new Mutex(false, Product.MutexName, out created)) return !created;
        }
        public static bool StopViewer(Action<string> say)
        {
            if (!ViewerRunning()) return true;
            say("Asking the running Agent Arena window to close…");
            try { using (var e = EventWaitHandle.OpenExisting(Product.ExitEvent)) e.Set(); } catch { }
            for (int i = 0; i < 40 && ViewerRunning(); i++) Thread.Sleep(250);
            if (!ViewerRunning()) { WaitForInstalledProcesses(say); return true; }
            foreach (var p in InstalledProcesses())
                try { p.Kill(); p.WaitForExit(5000); say("Stopped Agent Arena PID " + p.Id); } catch { }
            return !ViewerRunning();
        }
        // The viewer releases its mutex before its process has finished shutting down (WebView2 teardown),
        // and Windows keeps AgentArena.exe locked until it has. Wait for the process itself, then stop it.
        static System.Collections.Generic.List<Process> InstalledProcesses()
        {
            var mine = Process.GetCurrentProcess().Id; var found = new System.Collections.Generic.List<Process>();
            foreach (var p in Process.GetProcessesByName("AgentArena"))
                try { if (p.Id != mine && p.MainModule.FileName.StartsWith(Paths.Install, StringComparison.OrdinalIgnoreCase)) found.Add(p); } catch { }
            return found;
        }
        static void WaitForInstalledProcesses(Action<string> say)
        {
            foreach (var p in InstalledProcesses())
                try { if (!p.WaitForExit(10000)) { p.Kill(); p.WaitForExit(5000); say("Stopped Agent Arena PID " + p.Id + " (it did not finish closing)"); } } catch { }
        }
        // A file that was just released can stay locked for a moment (a closing process, an antivirus scan).
        static void DeleteTree(string dir)
        {
            for (int attempt = 1; ; attempt++)
                try { Directory.Delete(dir, true); return; }
                catch (IOException) { if (attempt >= 20) throw; Thread.Sleep(500); }
                catch (UnauthorizedAccessException) { if (attempt >= 20) throw; Thread.Sleep(500); }
        }

        [System.Runtime.InteropServices.DllImport("kernel32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)] static extern bool DeleteFile(string path);
        static void CopyTree(string from, string to)
        {
            foreach (var dir in Directory.GetDirectories(from, "*", SearchOption.AllDirectories)) Directory.CreateDirectory(dir.Replace(from, to));
            foreach (var file in Directory.GetFiles(from, "*", SearchOption.AllDirectories))
            {
                var dest = file.Replace(from, to); Directory.CreateDirectory(Path.GetDirectoryName(dest)); File.Copy(file, dest, true);
                DeleteFile(dest + ":Zone.Identifier");   // installed copies only; same effect as Unblock-File
            }
        }

        /// <summary>Install, repair or upgrade. Idempotent; every step is logged and reported.</summary>
        public static bool Install(InstallOptions o, DeploymentConfig deploy, Action<string> say)
        {
            Log.Info("install started: package " + Product.Version + " from " + PackageAppDir);
            say("Checking the package…");
            var problems = VerifyManifest(PackageAppDir);
            if (problems.Count > 0) { say("FAILED: the package is incomplete or modified (" + string.Join("; ", problems.Take(3)) + "). Re-copy the ZIP from the USB drive and extract all of it."); return false; }
            var release = Prerequisites.DotNetRelease(); int rel;
            if (!int.TryParse(release, out rel) || rel < 528040) { say("FAILED: .NET Framework 4.8 is required (found release " + (release ?? "none") + "). It is part of Windows 10 1903+ and Windows 11; ask IT to install it."); return false; }
            var wv = Prerequisites.WebView2Version();
            if (wv == null) { say("FAILED: the Microsoft Edge WebView2 Runtime is not installed. It is built into Windows 11. On Windows 10, ask IT to install the WebView2 Runtime (Evergreen), then run this installer again. Nothing was changed."); return false; }
            say("Prerequisites OK: .NET Framework release " + rel + ", WebView2 Runtime " + wv);
            if (o.Identity == null) { say("FAILED: no authorized display identity was detected for this computer; see the detection details above. Nothing was changed."); return false; }

            if (!StopViewer(say)) { say("FAILED: Agent Arena is still running and could not be closed. Close it and try again."); return false; }
            Directory.CreateDirectory(Paths.Install);
            var target = Path.Combine(Paths.Install, Product.Version); var partial = target + ".partial";
            say("Copying program files to " + target + " …");
            // Clear leftovers of any interrupted install, whichever version left them.
            foreach (var stale in Directory.GetDirectories(Paths.Install, "*.partial")) { Directory.Delete(stale, true); say("Removed an interrupted install: " + Path.GetFileName(stale)); }
            CopyTree(PackageAppDir.TrimEnd('\\'), partial);
            var copied = VerifyManifest(partial);
            if (copied.Count > 0) { say("FAILED: copied files did not verify (" + string.Join("; ", copied.Take(3)) + ")."); return false; }
            if (Directory.Exists(target)) { var old = target + ".old"; if (Directory.Exists(old)) Directory.Delete(old, true); Directory.Move(target, old); Directory.Move(partial, target); Directory.Delete(old, true); }
            else Directory.Move(partial, target);
            File.WriteAllText(Paths.CurrentPointer, Product.Version);
            say("Program files installed and verified.");

            LocalConfig previous = null; try { previous = LocalConfig.Load(); } catch { }
            var cfg = previous ?? new LocalConfig();
            cfg.Version = Product.Version; cfg.KeepAwake = o.KeepAwake; cfg.StartAtSignIn = o.StartAtSignIn; cfg.Telemetry = o.Telemetry;
            if (cfg.Endpoints.Count == 0 || previous == null) cfg.Endpoints = deploy.Endpoints.ToList();
            cfg.DisplayId = o.Identity.DisplayId; cfg.Role = o.Identity.Role;
            if (!string.IsNullOrWhiteSpace(o.EnrollmentCode))
            {
                say("Enrolling " + o.Identity.DisplayId + " with the event service at " + cfg.Endpoints[0] + " …");
                try
                {
                    string id, secret;
                    var r = ArenaClient.Enroll(cfg.Endpoints[0], o.Identity.DisplayId, o.EnrollmentCode, out id, out secret);
                    CredentialStore.Save(id, secret);
                    cfg.LocationId = Json.Str(r, "location_id"); cfg.Role = Json.Str(r, "role"); cfg.ServiceId = Json.Str(r, "service_id");
                    var eps = Json.List(r, "endpoints"); if (eps.Count > 0) cfg.Endpoints = eps;
                    say("Enrolled: credential " + CredentialStore.Redact(id) + " stored for this Windows user (DPAPI). Location " + cfg.LocationId + ".");
                }
                catch (ArenaException ex) { say("ENROLLMENT FAILED (" + ex.Code + "): " + ex.Message + ". The program is installed; get a new code from the coordinator and run INSTALL again."); }
                catch (Exception ex) { say("ENROLLMENT FAILED: event service unreachable at " + cfg.Endpoints[0] + " (" + ex.Message + "). The program is installed; run INSTALL again when the service is up."); }
            }
            else
            {
                string id, secret;
                if (CredentialStore.TryLoad(out id, out secret)) say("Keeping the existing viewer credential " + CredentialStore.Redact(id) + ".");
                else say("Not enrolled yet: no enrollment code was entered. Arena will show 'not enrolled' until you run INSTALL with a code.");
            }
            cfg.Save(); say("Configuration saved: " + Paths.ConfigFile);

            var exe = Path.Combine(target, "AgentArena.exe");
            if (o.StartAtSignIn) { Startup.Enable(exe); say("Sign-in startup: one entry (HKCU Run \"" + Product.RunValue + "\")."); }
            else { Startup.Disable(); say("Sign-in startup: off."); }
            try { Shortcuts(exe, o.DesktopShortcut); say("Start menu shortcuts created" + (o.DesktopShortcut ? " and a desktop shortcut." : ".")); } catch (Exception ex) { say("Start menu shortcuts not created: " + ex.Message); }
            if (o.RetireLegacy && Legacy.Inventory().Any(l => l.Owned && l.Kind != "browser-profile" || o.DeleteLegacyBrowserData))
            {
                say("Retiring the original Phase 4.2 viewer (owned items only)…");
                foreach (var line in Legacy.Retire(o.DeleteLegacyBrowserData)) say("  " + line);
            }
            var older = Directory.GetDirectories(Paths.Install).Select(d => { Version v; return Version.TryParse(Path.GetFileName(d), out v) ? Tuple.Create(v, d) : null; })
                .Where(x => x != null && x.Item1 < new Version(Product.Version)).OrderByDescending(x => x.Item1).Skip(1);   // keep one previous version
            foreach (var x in older) try { Directory.Delete(x.Item2, true); say("Removed old version " + x.Item1); } catch { }
            Log.Info("install finished");
            return true;
        }

        static string DesktopLink { get { return Path.Combine(Paths.DesktopFolder, "Agent Arena.lnk"); } }
        static void Shortcuts(string exe, bool desktop)
        {
            var folder = Path.Combine(Paths.ProgramsFolder, "Agent Arena");
            Directory.CreateDirectory(folder);
            dynamic shell = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell"));
            foreach (var item in new[] { Tuple.Create("Agent Arena", ""), Tuple.Create("Agent Arena Diagnostics", "--diagnostics"), Tuple.Create("Agent Arena Setup (repair or uninstall)", "--install") })
            {
                dynamic link = shell.CreateShortcut(Path.Combine(folder, item.Item1 + ".lnk"));
                link.TargetPath = exe; link.Arguments = item.Item2; link.WorkingDirectory = Path.GetDirectoryName(exe); link.IconLocation = exe + ",0"; link.Save();
            }
            if (desktop)
            {
                Directory.CreateDirectory(Paths.DesktopFolder);
                dynamic d = shell.CreateShortcut(DesktopLink); d.TargetPath = exe; d.WorkingDirectory = Path.GetDirectoryName(exe); d.IconLocation = exe + ",0"; d.Description = "Agent Arena"; d.Save();
            }
            else RemoveDesktopLink();
        }

        public static bool Uninstall(bool removeData, Action<string> say)
        {
            Log.Info("uninstall started");
            if (!StopViewer(say)) { say("FAILED: Agent Arena is still running; nothing was removed. Close it and try again."); return false; }
            Startup.Disable(); say("Sign-in startup removed.");
            if (RemoveDesktopLink()) say("Desktop shortcut removed.");
            var folder = Path.Combine(Paths.ProgramsFolder, "Agent Arena");
            if (Directory.Exists(folder)) { Directory.Delete(folder, true); say("Start menu shortcuts removed."); }
            var self = Paths.ExeDir.TrimEnd('\\');
            if (Directory.Exists(Paths.Install))
                foreach (var dir in Directory.GetDirectories(Paths.Install))
                {
                    if (dir.Equals(self, StringComparison.OrdinalIgnoreCase)) { say("Note: " + dir + " is in use by this setup window; it is removed when you close it."); ScheduleSelfDelete(dir); continue; }
                    string busy = null;
                    try { DeleteTree(dir); }
                    catch (IOException ex) { busy = ex.Message; }
                    catch (UnauthorizedAccessException ex) { busy = ex.Message; }
                    if (busy != null) { Log.Error("uninstall: " + busy); say("FAILED: " + dir + " is still in use (" + busy + "). Close Agent Arena and try again."); return false; }
                }
            if (File.Exists(Paths.CurrentPointer)) File.Delete(Paths.CurrentPointer);
            if (Directory.Exists(Paths.Install) && !Directory.EnumerateFileSystemEntries(Paths.Install).Any()) Directory.Delete(Paths.Install);
            say("Program files removed.");
            if (removeData)
            {
                foreach (var f in new[] { Paths.CredentialFile, Paths.ConfigFile, Paths.WindowFile }) if (File.Exists(f)) File.Delete(f);
                if (Directory.Exists(Paths.WebViewData)) Directory.Delete(Paths.WebViewData, true);
                say("Stored credential, configuration and Arena browser data removed. Ask the coordinator administrator to revoke this display's credential.");
            }
            else say("Kept for a later reinstall: viewer credential (DPAPI), configuration and Arena browser data in " + Paths.Data + ".");
            if (ViewerRunning()) { say("FAILED: an Agent Arena process is still running."); return false; }
            say("Ollama, models, network settings, power settings and other applications were not touched.");
            Log.Info("uninstall finished");
            return true;
        }
        static bool RemoveDesktopLink()
        {
            // Only our own shortcut: it must point into the Agent Arena program folder.
            if (!File.Exists(DesktopLink)) return false;
            try { dynamic shell = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell")); string target = shell.CreateShortcut(DesktopLink).TargetPath;
                  if (target.StartsWith(Paths.Install, StringComparison.OrdinalIgnoreCase)) { File.Delete(DesktopLink); return true; } }
            catch { }
            return false;
        }
        static void ScheduleSelfDelete(string dir)
        {
            var cmd = "/c ping -n 4 127.0.0.1 >nul & rmdir /s /q \"" + dir + "\"";
            Process.Start(new ProcessStartInfo("cmd.exe", cmd) { CreateNoWindow = true, UseShellExecute = false, WindowStyle = ProcessWindowStyle.Hidden });
        }
    }

    public class InstallerForm : Form
    {
        readonly DeploymentConfig deploy;
        readonly TextBox info = new TextBox(), progress = new TextBox(), code = new TextBox();
        readonly ComboBox identity = new ComboBox();
        readonly CheckBox desktopLink = new CheckBox(), startup = new CheckBox(), awake = new CheckBox(), telemetry = new CheckBox(), retire = new CheckBox(), wipeLegacy = new CheckBox(), wipeData = new CheckBox();
        readonly Button install = new Button(), launch = new Button(), diagnostics = new Button(), uninstall = new Button(), close = new Button();
        readonly Label status = new Label();
        List<Candidate> candidates;

        public InstallerForm(DeploymentConfig deploy, string prefillCode = null)
        {
            this.deploy = deploy;
            if (!string.IsNullOrWhiteSpace(prefillCode)) code.Text = prefillCode.Trim();   // from the USB, so nobody retypes it
            Text = Product.Name + " " + Product.Version + " — Setup"; Size = new Size(940, 760); MinimumSize = new Size(820, 640);
            StartPosition = FormStartPosition.CenterScreen; AutoScaleDimensions = new SizeF(96f, 96f); AutoScaleMode = AutoScaleMode.Dpi; Font = new Font("Segoe UI", 9.5f);
            Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
            var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, Padding = new Padding(12) };
            var title = new Label { Text = "INSTALL AGENT ARENA  ·  version " + Product.Version, Font = new Font("Segoe UI Semibold", 15f), AutoSize = true };
            info.Multiline = true; info.ReadOnly = true; info.ScrollBars = ScrollBars.Vertical; info.Font = new Font("Consolas", 9f); info.Height = 250; info.Dock = DockStyle.Fill;
            var form = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, FlowDirection = FlowDirection.LeftToRight, WrapContents = true };
            identity.DropDownStyle = ComboBoxStyle.DropDownList; identity.Width = 420;
            code.Width = 260; code.CharacterCasing = CharacterCasing.Upper;
            form.Controls.Add(new Label { Text = "Detected identity:", AutoSize = true, Padding = new Padding(0, 6, 0, 0) }); form.Controls.Add(identity);
            form.Controls.Add(new Label { Text = "Enrollment code (from the coordinator):", AutoSize = true, Padding = new Padding(12, 6, 0, 0) }); form.Controls.Add(code);
            var opts = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true };
            Setup(desktopLink, "Desktop shortcut", true); Setup(startup, "Start Agent Arena when I sign in", true); Setup(awake, "Keep the display awake while Arena is open", true);
            Setup(telemetry, "Report this head's CPU/RAM/GPU readings", true); Setup(retire, "Retire the original Phase 4.2 viewer if found", true);
            Setup(wipeLegacy, "…and delete its old browser data", false); Setup(wipeData, "When uninstalling, also remove the stored credential", false);
            foreach (var c in new[] { desktopLink, startup, awake, telemetry, retire, wipeLegacy, wipeData }) opts.Controls.Add(c);
            var buttons = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true };
            Setup(install, "Install"); Setup(launch, "Launch Arena"); Setup(diagnostics, "Diagnostics"); Setup(uninstall, "Uninstall"); Setup(close, "Close");
            foreach (var b in new[] { install, launch, diagnostics, uninstall, close }) buttons.Controls.Add(b);
            progress.Multiline = true; progress.ReadOnly = true; progress.ScrollBars = ScrollBars.Vertical; progress.Font = new Font("Consolas", 9f); progress.Dock = DockStyle.Fill;
            status.AutoSize = true; status.Font = new Font("Segoe UI Semibold", 11f);
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize)); layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 260));
            for (int i = 0; i < 3; i++) layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100)); layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            foreach (Control c in new Control[] { title, info, form, opts, buttons, progress, status }) layout.Controls.Add(c);
            Controls.Add(layout);
            install.Click += delegate { Run(true); }; uninstall.Click += delegate { Run(false); }; close.Click += delegate { Close(); };
            launch.Click += delegate { var dir = Paths.CurrentVersionDir(); if (dir != null) Process.Start(Path.Combine(dir, "AgentArena.exe")); else Say("Not installed yet."); };
            diagnostics.Click += delegate { new DiagnosticsForm(deploy).Show(this); };
            Shown += delegate { Detect(); status.Text = "Ready. Check the detected identity, enter the enrollment code, then click Install."; };
        }
        static void Setup(CheckBox c, string text, bool on) { c.Text = text; c.Checked = on; c.AutoSize = true; c.Margin = new Padding(3, 3, 18, 3); }
        static void Setup(Button b, string text) { b.Text = text; b.AutoSize = true; b.Padding = new Padding(10, 4, 10, 4); b.Margin = new Padding(3, 6, 8, 6); }
        // Logged on the calling thread, before the UI hop, so the log keeps the engine's order (field defect D5).
        void Say(string line) { Log.Info("setup: " + line); Show(line); }
        void Show(string line) { if (InvokeRequired) { BeginInvoke(new Action<string>(Show), line); return; } progress.AppendText(line + Environment.NewLine); }

        void Detect()
        {
            var lines = new List<string>();
            lines.Add("Computer: " + Environment.MachineName + "   Windows user: " + Environment.UserName + "   " + Environment.OSVersion.VersionString);
            var endpoints = Identity.Endpoints(deploy);
            lines.Add("Event service: " + string.Join(", ", endpoints) + (endpoints == deploy.Endpoints ? " (from the package)" : " (from the installed configuration)"));
            lines.Add("Local IPv4 addresses:");
            foreach (var a in Identity.LocalAddresses()) lines.Add("   " + a.Item1.PadRight(16) + " " + a.Item3.PadRight(6) + " " + a.Item2);
            var notes = new List<string>();
            candidates = Identity.Resolve(deploy, endpoints, notes);
            foreach (var n in notes) lines.Add("Identity note: " + n);
            identity.Items.Clear();
            foreach (var c in candidates) identity.Items.Add(c.DisplayId + "  (" + c.Role + ", " + c.Address + " on " + c.Interface + ")");
            if (candidates.Count == 0)
            {
                lines.Add("IDENTITY: none. This computer is not a registered display at the event service, and no address is in an authorized display network (" + string.Join(", ", deploy.DisplayNetworks) + ").");
                lines.Add("          Installation is blocked; nothing will be guessed. See the identity notes above.");
                install.Enabled = false;
            }
            else
            {
                identity.SelectedIndex = 0;
                lines.Add(candidates.Count == 1 ? "IDENTITY: " + candidates[0].DisplayId + " (" + candidates[0].Role + ") — " + candidates[0].Reason
                                                : "IDENTITY: " + candidates.Count + " possible identities — choose the one for this computer (multiple network interfaces).");
                lines.Add("          Detection only proposes the identity. The one-time enrollment code authorizes it.");
            }
            string summary; Engine.DetectState(out summary);
            lines.Add("Existing installation: " + summary);
            lines.Add("Prerequisites: .NET Framework release " + (Prerequisites.DotNetRelease() ?? "missing") + "; WebView2 Runtime " + (Prerequisites.WebView2Version() ?? "MISSING"));
            try { var h = ArenaClient.Health(endpoints[0], 10); lines.Add("Event service: reachable, service_id " + Json.Str(h, "service_id") + ", version " + Json.Str(h, "version")); }
            catch (Exception ex) { lines.Add("Event service: NOT reachable at " + endpoints[0] + " (" + ex.Message + "). You can install now and enroll later."); }
            lines.Add("Install folder: " + Path.Combine(Paths.Install, Product.Version) + "    Log: " + Log.FilePath);
            info.Text = string.Join(Environment.NewLine, lines);
            install.Text = Paths.CurrentVersionDir() == null ? "Install" : "Repair / Upgrade";
        }

        void Run(bool installing)
        {
            foreach (var b in new[] { install, uninstall, launch }) b.Enabled = false;
            status.Text = installing ? "Installing…" : "Uninstalling…"; status.ForeColor = Color.Black;
            var o = new InstallOptions { Identity = identity.SelectedIndex >= 0 ? candidates[identity.SelectedIndex] : null, EnrollmentCode = code.Text.Trim(),
                StartAtSignIn = startup.Checked, KeepAwake = awake.Checked, Telemetry = telemetry.Checked, RetireLegacy = retire.Checked, DeleteLegacyBrowserData = wipeLegacy.Checked, DesktopShortcut = desktopLink.Checked };
            var removeData = wipeData.Checked;
            new Thread(() =>
            {
                bool ok;
                try { ok = installing ? Engine.Install(o, deploy, Say) : Engine.Uninstall(removeData, Say); }
                catch (Exception ex) { Say("FAILED: " + ex.Message); Log.Error(ex.ToString()); ok = false; }
                Engine.WriteResult(installing ? "install" : "uninstall", ok);
                BeginInvoke(new Action(() =>
                {
                    status.Text = ok ? (installing ? "SUCCESS — Agent Arena is installed. Click Launch Arena." : "SUCCESS — Agent Arena was uninstalled.")
                                     : "FAILED — read the messages above. Details: " + Log.FilePath;
                    status.ForeColor = ok ? Color.DarkGreen : Color.DarkRed;
                    foreach (var b in new[] { install, uninstall, launch }) b.Enabled = true;
                    Detect();
                }));
            }) { IsBackground = true }.Start();
        }
    }
}
