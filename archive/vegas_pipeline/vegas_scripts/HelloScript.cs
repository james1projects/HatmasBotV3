/*
 * ============================================================================
 *  HelloScript.cs  —  Vegas 21 `-SCRIPT:` sanity check (Risk R-2)
 * ============================================================================
 *
 *  PURPOSE
 *      Smallest possible Vegas script. Pops a MessageBox and exits. We use
 *      this to verify that Vegas Pro 21 honours the `-SCRIPT:` command-line
 *      switch. If this works, the whole orchestrator plan is viable. If it
 *      does not, we fall back to having James run scripts manually from
 *      Tools -> Scripting after the orchestrator opens Vegas.
 *
 *  TWO TESTS TO RUN (both from a Command Prompt / PowerShell window):
 *
 *  TEST A — Vegas closed, cold launch with a script:
 *      1. Close any open Vegas windows.
 *      2. Run:
 *         "C:\Program Files\VEGAS\VEGAS Pro 21.0\vegas210.exe" -SCRIPT:"C:\Users\james\HatmasBot\vegas_scripts\HelloScript.cs"
 *      3. Vegas should open, and a message box saying "HelloScript: Vegas
 *         21 -SCRIPT: is working." should appear.
 *
 *  TEST B — Vegas already open, re-use or new instance?
 *      1. Open Vegas normally first (any project, or empty).
 *      2. Run the same command as Test A in another CMD window.
 *      3. Observe:
 *          - Does the message box appear in the EXISTING Vegas window
 *            (the second launch routed the script to it)?
 *          - OR does a SECOND Vegas window open with the message box?
 *          - OR does the second launch fail / do nothing?
 *      This determines whether `process_vods.py` needs to refuse to run
 *      when Vegas is already open.
 *
 *  INSTALL
 *      The -SCRIPT: tests use an absolute path so this file does NOT need
 *      to be in Vegas's Script Menu folder. Just leave it at:
 *          C:\Users\james\HatmasBot\vegas_scripts\HelloScript.cs
 *
 *  NAMESPACE NOTE
 *      Vegas 14+ uses ScriptPortal.Vegas. Vegas 13 and earlier used
 *      Sony.Vegas. If this line fails to compile, swap the namespace.
 * ============================================================================
 */

using System;
using System.Windows.Forms;
using ScriptPortal.Vegas;   // Vegas 14+ (change to Sony.Vegas for Vegas 13)

public class EntryPoint
{
    public void FromVegas(Vegas vegas)
    {
        string versionInfo;
        try
        {
            versionInfo = "Vegas version: " + vegas.Version;
        }
        catch (Exception)
        {
            versionInfo = "Vegas version: (unknown — .Version threw)";
        }

        MessageBox.Show(
            "HelloScript: Vegas 21 -SCRIPT: is working.\r\n\r\n"
            + versionInfo + "\r\n"
            + "Script path: " + typeof(EntryPoint).Assembly.Location + "\r\n\r\n"
            + "If you can read this, we're good to proceed with the\r\n"
            + "orchestrator plan. Close this dialog and report back.",
            "HelloScript — OK",
            MessageBoxButtons.OK,
            MessageBoxIcon.Information
        );
    }
}
