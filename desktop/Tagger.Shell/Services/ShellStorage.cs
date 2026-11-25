using System.IO;
using System.Security.Cryptography;
using System.Text;

namespace Tagger.Shell.Services;

internal static class ShellStorage
{
    internal static IEnumerable<string> Folders(string? root)
    {
        yield return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Tagger");
        if (!string.IsNullOrEmpty(root)) yield return Path.Combine(root, "run");
        var key = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(root ?? AppContext.BaseDirectory)))[..16];
        yield return Path.Combine(Path.GetTempPath(), "Tagger", key);
    }

    internal static IEnumerable<string> Profiles(string root)
    {
        foreach (var folder in Folders(root)) yield return Path.Combine(folder, "WebView2");
    }

    internal static void PrepareProfile(string path)
    {
        Directory.CreateDirectory(path);
        using var probe = new FileStream(Path.Combine(path, ".write-" + Guid.NewGuid().ToString("N")),
            FileMode.CreateNew, FileAccess.ReadWrite, FileShare.None, 1, FileOptions.DeleteOnClose);
        probe.WriteByte(0);
    }
}
