using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Windows;
using System.Windows.Input;
using System.Windows.Threading;
using Microsoft.Web.WebView2.Core;
using Tagger.Shell.Services;

namespace Tagger.Shell;

public partial class MainWindow : Window
{
    private PythonServer? _server;
    private bool _webViewReady;
    private bool _booting;
    private bool _reallyClose;

    private CancellationTokenSource? _installCts;
    private bool _installing;

    // 安装输出走批量刷新：pip 一次能刷出几百行，逐行 BeginInvoke 会把
    // 界面卡成幻灯片（而它正是这时候最需要保持可响应的）。
    private readonly StringBuilder _pendingLog = new();
    private readonly object _pendingLock = new();
    private readonly DispatcherTimer _logTimer;

    public MainWindow()
    {
        InitializeComponent();
        Loaded += OnLoaded;
        Closing += OnClosing;
        PreviewKeyDown += OnPreviewKeyDown;

        _logTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(150) };
        _logTimer.Tick += (_, _) => FlushInstallLog();
    }

    // ---- 启动

    private async void OnLoaded(object sender, RoutedEventArgs e)
    {
        await BootAsync();
    }

    private async Task BootAsync()
    {
        if (_booting) return;
        _booting = true;

        ShowLoading("正在启动本地服务…", "", "首次启动要加载 Python 依赖，可能需要十几秒。");

        try
        {
            _server = PythonServer.Locate();
            AppLog.Write($"root={_server.Root} python={_server.PythonPath}");
            StatusDetail.Text = $"Python：{Path.GetFileName(_server.PythonPath)}";

            _server.Output += OnServerOutput;
            await _server.StartAsync();
            AppLog.Write($"服务已就绪 {_server.Url}");

            ShowLoading("正在载入界面…", _server.Url, "");
            await EnsureWebViewAsync();

            Web.Source = new Uri(_server.Url);
        }
        catch (WebView2RuntimeNotFoundException ex)
        {
            // 没装 WebView2 运行时不等于不能用：服务已经起来了，交给默认浏览器。
            AppLog.Write(ex, "WebView2 运行时缺失");
            ShowError(
                "这台机器没有 WebView2 运行时，窗口里没法内嵌网页。\n" +
                "本地服务已经在跑，已用系统浏览器打开同一个地址；" +
                "想让窗口内嵌，安装 Microsoft Edge WebView2 Runtime 即可。",
                ex.Message);
            // 顺手给个下载入口——比让人自己去搜"WebView2 运行时"省事。
            RuntimeCheck.OfferWebView2Download();
            if (_server is not null)
            {
                try { OpenExternal(_server.Url); }
                catch (Exception openEx) { AppLog.Write(openEx, "打开浏览器失败"); }
            }
        }
        catch (EnvNotInitializedException ex)
        {
            // 这个错误有唯一的解：装一次环境。别的错误（端口占用、服务崩了）
            // 点安装只会白等十分钟，所以按钮只在这一条分支上出现。
            AppLog.Write(ex, "环境未初始化");
            ShowError(ex.Message, "", canInstall: true);
        }
        catch (Exception ex)
        {
            AppLog.Write(ex, "启动失败");
            var log = _server?.LogTail ?? "";
            // 环境装到一半、或有人把 env\python 删了：服务能起来但 cli 报
            // 缺依赖。这种也能用安装按钮修。
            ShowError(ex.Message, log, canInstall: log.Contains("缺 web 依赖"));
        }
        finally
        {
            _booting = false;
        }
    }

    private async Task EnsureWebViewAsync()
    {
        if (_webViewReady) return;

        var userData = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "Tagger", "WebView2");
        Directory.CreateDirectory(userData);

        var env = await CoreWebView2Environment.CreateAsync(null, userData);
        await Web.EnsureCoreWebView2Async(env);

        var core = Web.CoreWebView2;

        // 页面里没有外链，干脆禁掉新窗口；真点了就在自己这里开。
        core.Settings.AreDefaultContextMenusEnabled = true;
        core.Settings.IsStatusBarEnabled = false;
        core.Settings.AreDevToolsEnabled = true;
        core.Settings.IsSwipeNavigationEnabled = false;

        core.NewWindowRequested += (_, e) =>
        {
            e.Handled = true;
            if (Uri.TryCreate(e.Uri, UriKind.Absolute, out var uri)) OpenExternal(uri.ToString());
        };

        core.NavigationCompleted += (_, e) =>
        {
            if (e.IsSuccess)
            {
                Web.Visibility = Visibility.Visible;
                Overlay.Visibility = Visibility.Collapsed;
            }
            else
            {
                ShowError("页面载入失败。本地服务可能已经退出。", e.WebErrorStatus.ToString());
            }
        };

        // 渲染进程崩溃时窗口会一直白屏，必须给个说法。
        core.ProcessFailed += (_, e) =>
        {
            AppLog.Write($"WebView2 进程异常：{e.ProcessFailedKind}");
            if (e.ProcessFailedKind == CoreWebView2ProcessFailedKind.RenderProcessExited ||
                e.ProcessFailedKind == CoreWebView2ProcessFailedKind.BrowserProcessExited)
            {
                ShowError("界面进程崩了。可以点重试重新载入。", e.ProcessFailedKind.ToString());
            }
        };

        _webViewReady = true;
    }

    private void OnServerOutput(string line)
    {
        // 子进程输出可能来自非 UI 线程
        Dispatcher.BeginInvoke(() =>
        {
            if (StatusDetail is not null && Overlay.Visibility == Visibility.Visible)
                StatusDetail.Text = line.Length > 120 ? line[..120] : line;
        });
    }

    // ---- 遮罩状态

    private void ShowLoading(string status, string detail, string hint)
    {
        ErrorPanel.Visibility = Visibility.Collapsed;
        LoadingPanel.Visibility = Visibility.Visible;
        Overlay.Visibility = Visibility.Visible;
        InstallPanel.Visibility = Visibility.Collapsed;
        StatusText.Text = status;
        StatusDetail.Text = detail;
        HintText.Text = hint;
    }

    private void ShowError(string message, string log, bool canInstall = false)
    {
        LoadingPanel.Visibility = Visibility.Collapsed;
        ErrorPanel.Visibility = Visibility.Visible;
        Overlay.Visibility = Visibility.Visible;
        ErrorText.Text = message;
        LogText.Text = string.IsNullOrWhiteSpace(log) ? "（没有更多输出）" : log;
        InstallPanel.Visibility = canInstall ? Visibility.Visible : Visibility.Collapsed;
    }

    // ---- 初始化环境（首次运行 / 环境残缺）

    private async void OnInstallClick(object sender, RoutedEventArgs e)
    {
        if (_installing) return;

        var root = PythonServer.FindRoot();
        if (root is null)
        {
            MessageBox.Show(this,
                "找不到 tagger 仓库根目录。\n\n" +
                "把 Tagger.Shell.exe 放进仓库里，或用 TAGGER_ROOT 指向它。",
                "初始化", MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }

        _installing = true;
        InstallButton.IsEnabled = false;
        InstallButton.Content = "正在安装…";
        InstallStage.Text = "准备中…";
        InstallProgress.Visibility = Visibility.Visible;
        RetryButton.IsEnabled = false;
        LogText.Text = "";
        LogScroll.ScrollToEnd();

        AppLog.Write($"开始初始化环境：{root}");

        var installer = new EnvInstaller();
        installer.Output += OnInstallOutput;
        installer.StageChanged += OnInstallStageChanged;
        _installCts = new CancellationTokenSource();

        _logTimer.Start();
        bool ok;
        try
        {
            ok = await installer.RunAsync(root, _installCts.Token);
        }
        catch (Exception ex)
        {
            AppLog.Write(ex, "初始化失败");
            AppendLine($"[界面] 初始化过程异常：{ex.Message}");
            ok = false;
        }
        finally
        {
            _logTimer.Stop();
            FlushInstallLog();
            installer.Output -= OnInstallOutput;
            installer.StageChanged -= OnInstallStageChanged;
            _installCts?.Dispose();
            _installCts = null;
            _installing = false;
        }

        InstallProgress.Visibility = Visibility.Collapsed;

        if (ok)
        {
            InstallStage.Text = "装好了，正在启动…";
            InstallButton.Content = "已完成";
            await Task.Delay(500);
            RetryButton.IsEnabled = true;
            InstallButton.IsEnabled = true;
            InstallButton.Content = "重新初始化";
            await BootAsync();
        }
        else
        {
            // 失败要让日志留在眼前：pip 的报错（网络、权限、版本不匹配）
            // 都在这块里，复制日志按钮能直接给出去。
            InstallStage.Text = "没有装完，日志在上面";
            InstallButton.Content = "重试初始化";
            InstallButton.IsEnabled = true;
            RetryButton.IsEnabled = true;
        }
    }

    private void OnInstallOutput(string line)
    {
        if (string.IsNullOrWhiteSpace(line)) return;
        lock (_pendingLock) _pendingLog.AppendLine(line);
    }

    private void OnInstallStageChanged(string stage) =>
        Dispatcher.BeginInvoke(() => InstallStage.Text = stage);

    private void AppendLine(string line)
    {
        lock (_pendingLock) _pendingLog.AppendLine(line);
        FlushInstallLog();
    }

    /// <summary>把攒下的输出一次性贴进日志框，然后滚到底。</summary>
    private void FlushInstallLog()
    {
        string chunk;
        lock (_pendingLock)
        {
            if (_pendingLog.Length == 0) return;
            chunk = _pendingLog.ToString();
            _pendingLog.Clear();
        }
        LogText.AppendText(chunk);
        LogScroll.ScrollToEnd();
    }

    // ---- 出错面板按钮

    private async void OnRetryClick(object sender, RoutedEventArgs e)
    {
        await ShutdownServerAsync();
        Web.Visibility = Visibility.Collapsed;
        await BootAsync();
    }

    private void OnCopyClick(object sender, RoutedEventArgs e)
    {
        try
        {
            Clipboard.SetText($"{(ErrorText.Text)}\n\n{LogText.Text}");
        }
        catch (Exception ex)
        {
            AppLog.Write(ex, "复制到剪贴板失败");
        }
    }

    private void OnOpenBrowserClick(object sender, RoutedEventArgs e)
    {
        if (_server is not null)
        {
            OpenExternal(_server.Url);
            return;
        }
        try
        {
            // 服务没起来时也让人能看一眼浏览器——说不定浏览器能给出更多线索
            OpenExternal("http://127.0.0.1:27100/");
        }
        catch (Exception ex)
        {
            AppLog.Write(ex, "打开浏览器失败");
        }
    }

    private void OnExitClick(object sender, RoutedEventArgs e)
    {
        // 走正常 Closing 流程，让服务被收掉。
        Close();
    }

    private static void OpenExternal(string url)
    {
        Process.Start(new ProcessStartInfo(url) { UseShellExecute = true })?.Dispose();
    }

    // ---- 快捷键

    private void OnPreviewKeyDown(object sender, KeyEventArgs e)
    {
        if (!_webViewReady) return;

        if (e.Key == Key.F5)
        {
            Web.CoreWebView2.Reload();
            e.Handled = true;
        }
        else if (e.Key == Key.F12 ||
                 (e.Key == Key.I && Keyboard.Modifiers == (ModifierKeys.Control | ModifierKeys.Shift)))
        {
            Web.CoreWebView2.OpenDevToolsWindow();
            e.Handled = true;
        }
    }

    // ---- 收尾

    private async void OnClosing(object? sender, CancelEventArgs e)
    {
        // 第一次关闭：先拦下，把 Python 进程树收干净，再走正常关闭流程。
        // 收完不能直接 Close()——那是在 Closing 事件里重入 Close，WPF 会抛
        // "在窗口关闭期间无法…"；交给 Shutdown 触发第二轮 Closing，此时
        // _reallyClose 已为 true，直接放行。
        if (_reallyClose) return;

        // e.Cancel 必须**在第一个 await 之前**置上：WPF 是在事件处理函数
        // 同步返回时读取 e.Cancel 的，一旦先 await，窗口就已经关掉了，
        // 后面再拦也没用（对话框根本来不及出现）。
        e.Cancel = true;

        // 正在打标时关窗会杀掉后端进程。产物是原子落盘的、重跑会跳过已完成，
        // 所以不算数据损失，但用户可能只是手滑——问一句比自己承担便宜。
        if (await IsJobRunningAsync())
        {
            var answer = MessageBox.Show(
                this,
                "还有任务正在打标。\n\n关闭窗口会中断它（已完成的图都已落盘，重新开始会跳过）。\n确定要关闭吗？",
                "任务进行中",
                MessageBoxButton.YesNo,
                MessageBoxImage.Warning,
                MessageBoxResult.No);
            if (answer != MessageBoxResult.Yes) return;   // 已经取消过，窗口留着
        }

        _reallyClose = true;

        // 安装跑到一半关窗 = 用户主动放弃。取消后子进程树会被收掉，
        // 不留下的半成品 env\python 会由下次 install 的幂等逻辑补齐。
        try { _installCts?.Cancel(); } catch (ObjectDisposedException) { /* 刚装完 */ }

        if (Overlay.Visibility == Visibility.Visible)
        {
            StatusText.Text = "正在停止本地服务…";
            StatusDetail.Text = "";
        }
        else
        {
            // 网页还在时也得让人知道为什么窗口没立刻消失。
            IsEnabled = false;
            Title = "Tagger · 正在停止服务…";
        }

        await ShutdownServerAsync();
        Application.Current.Shutdown();
    }

    /// <summary>
    /// 问一下后端有没有在跑的任务。查不到就当作没有——不能因为一个探测请求
    /// 失败就把用户关在窗口里出不去。
    /// </summary>
    private async Task<bool> IsJobRunningAsync()
    {
        if (_server is null) return false;
        try
        {
            using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(1.5) };
            var url = new Uri(new Uri(_server.Url), "api/health");
            var json = await http.GetStringAsync(url);
            using var doc = JsonDocument.Parse(json);
            return doc.RootElement.TryGetProperty("running_job", out var v)
                   && v.ValueKind == JsonValueKind.String
                   && !string.IsNullOrEmpty(v.GetString());
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or JsonException)
        {
            return false;
        }
    }

    private async Task ShutdownServerAsync()
    {
        var server = _server;
        _server = null;
        if (server is null) return;

        server.Output -= OnServerOutput;
        try
        {
            await server.KillAsync();
            AppLog.Write("服务已停止");
        }
        catch (Exception ex)
        {
            AppLog.Write(ex, "停止服务时出错");
        }
    }
}
