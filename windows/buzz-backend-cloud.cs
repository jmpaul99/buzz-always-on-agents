// Tiny PE launcher for Buzz Desktop. Desktop copies buzz-backend-* bytes into
// %TEMP%\buzz-provider-*\provider.exe and CreateProcess that copy, so a .cmd
// shim becomes an invalid image (Windows "Unsupported 16-Bit Application").
// Placeholders __PYTHON__, __IMPL__, and __PATH_EXTRA__ are filled by install-path.ps1.
using System;
using System.Diagnostics;
using System.Text;
using System.Threading;

internal static class Program
{
    private const string Python = @"__PYTHON__";
    private const string Impl = @"__IMPL__";
    private const string PathExtra = @"__PATH_EXTRA__";

    private static int Main(string[] args)
    {
        try
        {
            var processPath = Environment.GetEnvironmentVariable("PATH") ?? "";
            var userPath = Environment.GetEnvironmentVariable("PATH", EnvironmentVariableTarget.User) ?? "";
            var machinePath = Environment.GetEnvironmentVariable("PATH", EnvironmentVariableTarget.Machine) ?? "";
            Environment.SetEnvironmentVariable("PATH", string.Join(";", PathExtra, userPath, machinePath, processPath));

            var psi = new ProcessStartInfo
            {
                FileName = Python,
                Arguments = Quote(Impl) + FormatArgs(args),
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = new UTF8Encoding(false),
                StandardErrorEncoding = new UTF8Encoding(false),
            };
            using (var child = Process.Start(psi))
            {
                if (child == null)
                {
                    return Fail("failed to start python");
                }
                // Desktop writes JSON to our stdin and closes it. Inheritance
                // leaves that pipe on this process, so Python would hang.
                var stdoutThread = new Thread(() => child.StandardOutput.BaseStream.CopyTo(Console.OpenStandardOutput()));
                var stderrThread = new Thread(() => child.StandardError.BaseStream.CopyTo(Console.OpenStandardError()));
                stdoutThread.IsBackground = true;
                stderrThread.IsBackground = true;
                stdoutThread.Start();
                stderrThread.Start();
                using (var input = Console.OpenStandardInput())
                {
                    input.CopyTo(child.StandardInput.BaseStream);
                }
                child.StandardInput.Close();
                child.WaitForExit();
                stdoutThread.Join();
                stderrThread.Join();
                return child.ExitCode;
            }
        }
        catch (Exception ex)
        {
            return Fail(ex.Message);
        }
    }

    private static string FormatArgs(string[] args)
    {
        if (args == null || args.Length == 0)
        {
            return "";
        }
        var sb = new StringBuilder();
        foreach (var arg in args)
        {
            sb.Append(' ');
            sb.Append(Quote(arg));
        }
        return sb.ToString();
    }

    private static string Quote(string value)
    {
        return "\"" + (value ?? "").Replace("\"", "\\\"") + "\"";
    }

    private static int Fail(string error)
    {
        var payload = "{\"ok\":false,\"request_id\":\"\",\"error\":" + ToJsonString(error) + "}";
        Console.Out.Write(payload);
        Console.Out.Flush();
        return 1;
    }

    private static string ToJsonString(string value)
    {
        var sb = new StringBuilder("\"");
        foreach (var ch in value ?? "")
        {
            switch (ch)
            {
                case '\\': sb.Append("\\\\"); break;
                case '"': sb.Append("\\\""); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default:
                    if (ch < ' ')
                    {
                        sb.Append("\\u");
                        sb.Append(((int)ch).ToString("x4"));
                    }
                    else
                    {
                        sb.Append(ch);
                    }
                    break;
            }
        }
        sb.Append('"');
        return sb.ToString();
    }
}
