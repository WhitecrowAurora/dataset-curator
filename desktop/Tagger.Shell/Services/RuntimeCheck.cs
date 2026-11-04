using System.Diagnostics;
using System.Windows;
using Microsoft.Web.WebView2.Core;

namespace Tagger.Shell.Services;

/// <summary>
/// 启动前的最低环境自检。
///
/// 这里只做"程序已经跑起来之后还能补救的事"——.NET 运行时**完全缺失**时托管代码
/// 根本不会执行，那种情况由 apphost 自己弹框（Windows 上会给出下载链接），
/// 任何 C# 代码都来不及介入。所以本类的定位是：机器上装了运行时但版本够不着
/// （例如只有 .NET 6、被 rollForward 策略挡住），以及 WebView2 缺失这两类。
/// </summary>
internal static class RuntimeCheck
{
    /// <summary>项目目标框架 net8.0-windows；低版本运行时起不来 WPF。</summary>
    public const int RequiredDotNetMajor = 8;

    /// <summary>.NET 8 桌面运行时下载页（含 Windows x64 安装包）。</summary>
    public const string DotNetDownloadUrl = "https://dotnet.microsoft.com/download/dotnet/8.0";

    /// <summary>Microsoft Edge WebView2 Evergreen Runtime 官方下载入口。</summary>
    public const string WebView2DownloadUrl = "https://go.microsoft.com/fwlink/p/?LinkId=2124703";

    /// <summary>版本达标返回 true；否则弹窗、可选跳下载页，并让调用方退出。</summary>
    public static bool EnsureDotNet()
    {
        var version = Environment.Version;
        if (version.Major >= RequiredDotNetMajor) return true;

        var text =
            $"Tagger 需要 .NET {RequiredDotNetMajor} 桌面运行时（Desktop Runtime），" +
            $"这台机器上找到的是 .NET {version.Major}.{version.Minor}.{version.Build}。\n\n" +
            $"点\"确定\"打开微软官方下载页，装完再重新打开本程序。\n" +
            $"点\"取消\"直接退出。\n\n" +
            $"链接：{DotNetDownloadUrl}";

        var answer = MessageBox.Show(
            text,
            "Tagger · 缺少 .NET 运行时",
            MessageBoxButton.OKCancel,
            MessageBoxImage.Warning,
            MessageBoxResult.OK);

        if (answer == MessageBoxResult.OK) OpenUrl(DotNetDownloadUrl);
        return false;
    }

    /// <summary>WebView2 运行时是否可用。拿不到版本号就当作没装。</summary>
    public static string? GetWebView2Version()
    {
        try
        {
            return CoreWebView2Environment.GetAvailableBrowserVersionString();
        }
        catch (Exception ex) when (ex is WebView2RuntimeNotFoundException or DllNotFoundException)
        {
            return null;
        }
    }

    /// <summary>问一句要不要装 WebView2。只负责引导，不负责装。</summary>
    public static void OfferWebView2Download()
    {
        var answer = MessageBox.Show(
            "这台机器没有 Microsoft Edge WebView2 运行时，窗口里没法内嵌网页。\n\n" +
            "点\"确定\"打开微软官方下载页（约 2MB，装完重开本程序即可内嵌）。\n" +
            "点\"取消\"继续用系统浏览器打开界面。\n\n" +
            $"链接：{WebView2DownloadUrl}",
            "Tagger · 缺少 WebView2 运行时",
            MessageBoxButton.OKCancel,
            MessageBoxImage.Information,
            MessageBoxResult.Cancel);

        if (answer == MessageBoxResult.OK) OpenUrl(WebView2DownloadUrl);
    }

    private static void OpenUrl(string url)
    {
        try
        {
            Process.Start(new ProcessStartInfo(url) { UseShellExecute = true })?.Dispose();
        }
        catch (Exception ex)
        {
            // 打不开浏览器不是致命错误：链接已经印在上面，用户能自己复制。
            AppLog.Write(ex, "打开下载页失败");
        }
    }
}
