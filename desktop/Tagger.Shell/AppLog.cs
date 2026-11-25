using System.IO;
using System.Text;
using Tagger.Shell.Services;

namespace Tagger.Shell;

/// <summary>
/// 壳子自己的日志。和 Python 子进程的日志分开：一个是"窗口没能打开"，
/// 一个是"服务没能起来"，混在一起排错时会互相干扰。
/// </summary>
internal static class AppLog
{
    private static readonly object Gate = new();

    internal static string LogPath { get; private set; } = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "Tagger", "shell.log");

    internal static void Write(string message) => Write(null, message);

    internal static void Write(Exception? ex, string? message = null)
    {
        try
        {
            var sb = new StringBuilder()
                .Append(DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"))
                .Append("  ");
            if (!string.IsNullOrWhiteSpace(message)) sb.Append(message);
            if (ex is not null)
            {
                if (!string.IsNullOrWhiteSpace(message)) sb.AppendLine();
                sb.Append(ex);
            }
            sb.AppendLine();

            lock (Gate)
            {
                foreach (var folder in ShellStorage.Folders(PythonServer.FindRoot()))
                {
                    var path = Path.Combine(folder, "shell.log");
                    try
                    {
                        Directory.CreateDirectory(folder);
                        File.AppendAllText(path, sb.ToString(), Encoding.UTF8);
                        LogPath = path;
                        return;
                    }
                    catch (Exception error) when (error is IOException or UnauthorizedAccessException)
                    {
                        System.Diagnostics.Trace.WriteLine($"日志目录不可写：{path}：{error.Message}");
                    }
                }
                System.Diagnostics.Trace.WriteLine(sb.ToString());
            }
        }
        catch (IOException)
        {
            // 连日志都写不了就算了，不能在错误处理里再抛一次。
        }
        catch (UnauthorizedAccessException)
        {
        }
    }
}
