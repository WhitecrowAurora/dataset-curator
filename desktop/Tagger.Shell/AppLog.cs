using System.IO;
using System.Text;

namespace Tagger.Shell;

/// <summary>
/// 壳子自己的日志。和 Python 子进程的日志分开：一个是"窗口没能打开"，
/// 一个是"服务没能起来"，混在一起排错时会互相干扰。
/// </summary>
internal static class AppLog
{
    private static readonly object Gate = new();

    internal static string LogPath { get; } = Path.Combine(
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
                Directory.CreateDirectory(Path.GetDirectoryName(LogPath)!);
                File.AppendAllText(LogPath, sb.ToString(), Encoding.UTF8);
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
