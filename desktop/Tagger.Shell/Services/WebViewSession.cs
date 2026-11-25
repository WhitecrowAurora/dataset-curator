using System.IO;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;

namespace Tagger.Shell.Services;

/// <summary>管理一个窗口的浏览器；缓存不可用时使用备用目录，退出时释放浏览器。</summary>
internal sealed class WebViewSession(Grid host, IEnumerable<string>? profiles = null)
{
    private CoreWebView2Environment? _environment;
    internal WebView2? View { get; private set; }

    internal async Task<CoreWebView2> InitializeAsync(string root, CancellationToken ct)
    {
        Exception? failure = null;
        foreach (var profile in profiles ?? ShellStorage.Profiles(root))
        {
            ct.ThrowIfCancellationRequested();
            try
            {
                ShellStorage.PrepareProfile(profile);
                AppLog.Write($"WebView2 初始化：{profile}");
                var view = new WebView2 { Visibility = Visibility.Collapsed };
                View = view;
                host.Children.Add(view);
                var pending = InitializeViewAsync(view, profile);
                try { await pending.WaitAsync(TimeSpan.FromSeconds(15), ct); }
                catch
                {
                    // SDK 初始化无法取消；释放控件并观察迟到的结果，避免未处理异常。
                    view.Dispose();
                    _ = ObserveAsync(pending);
                    throw;
                }
                ct.ThrowIfCancellationRequested();
                AppLog.Write($"WebView2 就绪：runtime={_environment!.BrowserVersionString} profile={profile}");
                return view.CoreWebView2;
            }
            catch (Exception ex) when (ex is UnauthorizedAccessException or IOException or COMException or TimeoutException)
            {
                failure = ex;
                AppLog.Write(ex, $"WebView2 初始化失败：{profile}，尝试备用目录");
                await CloseAsync();
            }
        }
        throw new IOException("内嵌浏览器无法初始化，已尝试备用缓存目录。可使用系统浏览器继续打标。", failure);
    }

    private async Task InitializeViewAsync(WebView2 view, string profile)
    {
        var environment = await CoreWebView2Environment.CreateAsync(null, profile,
            new CoreWebView2EnvironmentOptions { ExclusiveUserDataFolderAccess = true });
        if (View == view) _environment = environment;
        await view.EnsureCoreWebView2Async(environment);
    }

    private static async Task ObserveAsync(Task pending)
    {
        try { await pending; }
        catch (Exception ex) { AppLog.Write(ex, "已放弃的 WebView2 初始化结束"); }
    }

    internal async Task CloseAsync()
    {
        var environment = _environment;
        _environment = null;
        var view = View;
        View = null;
        if (view is null) return;
        var exited = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        void OnExited(object? sender, CoreWebView2BrowserProcessExitedEventArgs args) => exited.TrySetResult();
        if (environment is not null) environment.BrowserProcessExited += OnExited;
        try
        {
            host.Children.Remove(view);
            view.Dispose();
            if (environment is not null) await exited.Task.WaitAsync(TimeSpan.FromSeconds(2));
        }
        catch (TimeoutException) { AppLog.Write("WebView2 已释放，浏览器进程仍在退出；不阻塞关窗。"); }
        catch (Exception ex) { AppLog.Write(ex, "释放 WebView2 时出错"); }
        finally
        {
            if (environment is not null) environment.BrowserProcessExited -= OnExited;
        }
    }
}
