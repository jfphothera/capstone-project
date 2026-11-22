// Entry point: AgentArena.exe [--install | --diagnostics | --uninstall-quiet | --background] [--profile-root <dir>]
using System;
using System.IO;
using System.Linq;
using System.Threading;
using System.Windows.Forms;

[assembly: System.Reflection.AssemblyTitle("Agent Arena")]
[assembly: System.Reflection.AssemblyProduct("Agent Arena")]
// AssemblyVersion and BuildInfo.Version come from the generated BuildInfo.g.cs (Build-AgentArena.ps1 -Version).
// Without this stamp the in-box compiler's output runs in legacy mode and WinForms ignores DPI settings.
[assembly: System.Runtime.Versioning.TargetFramework(".NETFramework,Version=v4.8", FrameworkDisplayName = ".NET Framework 4.8")]

namespace AgentArena
{
    static class Program
    {
        [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr value);

        [STAThread]
        static int Main(string[] args)
        {
            // Per-monitor V2: sharp text at 125%/150%/200% and when moving between monitors.
            try { SetProcessDpiAwarenessContext(new IntPtr(-4)); } catch (EntryPointNotFoundException) { }
            var root = Arg(args, "--profile-root"); if (root != null) Paths.UseProfileRoot(root);
            unattended = new[] { "--install-quiet", "--uninstall-quiet", "--state", "--self-test", "--diagnostics-report" }.Any(args.Contains);
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            Application.ThreadException += (s, e) => Fatal(e.Exception);
            AppDomain.CurrentDomain.UnhandledException += (s, e) => Fatal(e.ExceptionObject as Exception);
            DeploymentConfig deploy = null;
            try { deploy = DeploymentConfig.Load(Arg(args, "--deployment") ?? Path.Combine(Paths.ExeDir, "arena-deployment.json")); }
            catch (Exception ex) { Log.Error("arena-deployment.json: " + ex.Message); }
            try
            {
                if (args.Contains("--install"))
                {
                    if (deploy == null) { MessageBox.Show("This package is incomplete: arena-deployment.json is missing. Re-extract the whole ZIP.", Product.Name); return 3; }
                    string prefill = null; var codeFile = Arg(args, "--code-file");
                    if (codeFile != null && File.Exists(codeFile)) prefill = File.ReadAllLines(codeFile).Select(l => l.Trim()).FirstOrDefault(l => l.StartsWith("ARENA-", StringComparison.OrdinalIgnoreCase));
                    Application.Run(new InstallerForm(deploy, prefill)); return 0;
                }
                if (args.Contains("--admin")) return Admin(args);
                if (args.Contains("--diagnostics")) { Application.Run(new DiagnosticsForm(deploy)); return 0; }
                if (args.Contains("--self-test")) return SelfTest();
                if (args.Contains("--state")) { string s; var st = Engine.DetectState(out s); Console.WriteLine(s); return st.Contains("installed") ? 0 : 1; }
                if (Arg(args, "--diagnostics-report") != null) { File.WriteAllText(Arg(args, "--diagnostics-report"), Diagnose.Report(Diagnose.Run(deploy))); return 0; }
                if (args.Contains("--install-quiet")) return Quiet(args, deploy, true);
                if (args.Contains("--uninstall-quiet")) return Quiet(args, deploy, false);
                return Viewer(args.Contains("--background"), deploy);
            }
            catch (Exception ex) { Fatal(ex); return 1; }
        }

        static string Arg(string[] args, string name) { var i = Array.IndexOf(args, name); return i >= 0 && i + 1 < args.Length ? args[i + 1] : null; }

        static bool unattended;   // quiet install/uninstall and the test switches: no windows, ever

        static void Fatal(Exception ex)
        {
            Log.Error("fatal: " + ex);
            if (unattended) { Console.WriteLine("FAILED: " + (ex == null ? "unknown" : ex.Message) + " (details in " + Log.FilePath + ")"); return; }
            MessageBox.Show("Agent Arena stopped because of an error:\n\n" + (ex == null ? "unknown" : ex.Message) + "\n\nDetails were saved to:\n" + Log.FilePath +
                            "\n\nRun AGENT ARENA DIAGNOSTICS for a full report.", Product.Name, MessageBoxButtons.OK, MessageBoxIcon.Error);
        }

        // The admin hub's screen. The exe sits in <hub app>\desktop; --hub-app names another folder (development).
        static int Admin(string[] args)
        {
            bool created;
            using (var mutex = new Mutex(true, AdminForm.MutexName, out created))
            {
                if (!created) { try { using (var e = EventWaitHandle.OpenExisting(AdminForm.ActivateEvent)) e.Set(); } catch { } return 0; }
                var hubApp = Arg(args, "--hub-app") ?? Path.GetFullPath(Path.Combine(Paths.ExeDir, ".."));
                if (!File.Exists(Path.Combine(hubApp, "arena_service", "admin_console.py")))
                {
                    MessageBox.Show("This is not an admin hub installation (" + hubApp + "). Run INSTALL-ADMIN-HUB.cmd from the hub USB.", Product.Name, MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return 2;
                }
                if (!File.Exists(Path.Combine(Paths.LocalAppData, "AgentArenaService", "aes.config.json")))
                {
                    MessageBox.Show("The event service is not configured on this computer. Run INSTALL-ADMIN-HUB.cmd from the hub USB.", Product.Name, MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return 2;
                }
                if (Prerequisites.WebView2Version() == null)
                {
                    MessageBox.Show("The Microsoft Edge WebView2 Runtime is missing, so the admin hub cannot be displayed. Ask IT to install it.", Product.Name, MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return 5;
                }
                Log.Info("admin hub window starting (" + hubApp + ")");
                var form = new AdminForm(hubApp);
                var activate = new EventWaitHandle(false, EventResetMode.AutoReset, AdminForm.ActivateEvent);
                new Thread(() => { while (true) { activate.WaitOne(); try { form.BeginInvoke(new Action(form.Activate2)); } catch { return; } } }) { IsBackground = true }.Start();
                Application.Run(form);
                Log.Info("admin hub window closed");
                return 0;
            }
        }

        static int Viewer(bool background, DeploymentConfig deploy)
        {
            bool created;
            using (var mutex = new Mutex(true, Product.MutexName, out created))
            {
                if (!created)
                {   // One viewer per user session: bring the existing window forward instead.
                    try { using (var e = EventWaitHandle.OpenExisting(Product.ActivateEvent)) e.Set(); } catch { }
                    return 0;
                }
                LocalConfig cfg;
                try { cfg = LocalConfig.Load(); }
                catch (Exception ex) { cfg = null; Log.Error("config: " + ex.Message); }
                if (cfg == null)
                {
                    if (background) return 2;
                    if (MessageBox.Show("Agent Arena is not installed or its configuration is damaged for this Windows user.\n\nOpen Diagnostics?", Product.Name, MessageBoxButtons.YesNo, MessageBoxIcon.Warning) == DialogResult.Yes)
                        Application.Run(new DiagnosticsForm(deploy));
                    return 2;
                }
                if (background && !cfg.StartAtSignIn) return 0;
                var web = Path.Combine(Paths.ExeDir, "web");
                var problems = Engine.VerifyManifest(Paths.ExeDir.TrimEnd('\\'));
                if (problems.Count > 0)
                {
                    Log.Error("integrity: " + string.Join("; ", problems));
                    MessageBox.Show("Agent Arena's installed files failed their integrity check (" + string.Join("; ", problems.Take(3)) + ").\n\nRun INSTALL AGENT ARENA to repair.", Product.Name, MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return 4;
                }
                if (Prerequisites.WebView2Version() == null)
                {
                    MessageBox.Show("The Microsoft Edge WebView2 Runtime is missing, so the Arena cannot be displayed. Ask IT to install it.", Product.Name, MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return 5;
                }
                Log.Info("viewer starting: display " + cfg.DisplayId + " role " + cfg.Role + (background ? " (sign-in)" : ""));
                var form = new ViewerForm(cfg, web);
                var activate = new EventWaitHandle(false, EventResetMode.AutoReset, Product.ActivateEvent);
                var exit = new EventWaitHandle(false, EventResetMode.AutoReset, Product.ExitEvent);
                new Thread(() => { while (true) { var i = WaitHandle.WaitAny(new WaitHandle[] { activate, exit }); try { if (i == 0) form.BeginInvoke(new Action(form.Activate2)); else { form.BeginInvoke(new Action(() => form.CloseFor("exit event (Agent Arena setup or uninstall asked the viewer to close)"))); return; } } catch { return; } } }) { IsBackground = true }.Start();
                Microsoft.Win32.SystemEvents.SessionEnding += (s, e) => Log.Info("Windows session ending: " + e.Reason);
                Application.Run(form);
                Log.Info("viewer exited (version " + Product.Version + ")");
                return 0;
            }
        }

        static int Quiet(string[] args, DeploymentConfig deploy, bool install)
        {
            // Same engine as the installer window, for automated tests and scripted repair.
            var notes = new System.Collections.Generic.List<string>();
            var candidates = Identity.Resolve(deploy, Identity.Endpoints(deploy), notes); var pick = Arg(args, "--identity");
            foreach (var n in notes) Console.WriteLine("Identity: " + n);
            var o = new InstallOptions { Identity = pick == null ? (candidates.Count == 1 ? candidates[0] : null) : candidates.FirstOrDefault(c => c.DisplayId == pick),
                EnrollmentCode = Arg(args, "--code"), StartAtSignIn = !args.Contains("--no-startup"), RetireLegacy = !args.Contains("--keep-legacy"),
                DeleteLegacyBrowserData = args.Contains("--delete-legacy-browser-data"), KeepAwake = !args.Contains("--no-keep-awake"), DesktopShortcut = !args.Contains("--no-desktop-shortcut") };
            bool ok;
            try { ok = install ? Engine.Install(o, deploy, Console.WriteLine) : Engine.Uninstall(args.Contains("--remove-data"), Console.WriteLine); }
            catch (Exception ex)
            {   // Unattended: report on the console and in the result file, never in a window nobody may be there to close.
                Log.Error("fatal: " + ex);
                Console.WriteLine("FAILED: " + ex.Message + " (details in " + Log.FilePath + ")");
                ok = false;
            }
            Engine.WriteResult(install ? "install" : "uninstall", ok);
            Console.WriteLine(ok ? "RESULT success" : "RESULT failed");
            return ok ? 0 : 1;
        }

        static int SelfTest()
        {
            // Non-interactive checks for the automated workstation tests.
            Console.WriteLine("version " + Product.Version);
            Console.WriteLine("webview2 " + (Prerequisites.WebView2Version() ?? "missing"));
            var problems = Engine.VerifyManifest(Paths.ExeDir.TrimEnd('\\'));
            Console.WriteLine("manifest " + (problems.Count == 0 ? "ok" : string.Join("; ", problems)));
            string summary; Engine.DetectState(out summary); Console.WriteLine("state " + summary.Replace(Environment.NewLine, " | "));
            return problems.Count == 0 ? 0 : 4;
        }
    }
}
