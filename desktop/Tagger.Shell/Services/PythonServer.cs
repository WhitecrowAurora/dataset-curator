using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Sockets;
using System.Text;

namespace Tagger.Shell.Services;

/// <summary>
/// 托管一次 `python -m tagger.cli web`。
///
/// 存在的意义只有一个：用户双击 exe 就能用，不需要先开终端敲命令。
/// 所以这里不做任何"聪明"的事——不接管日志分析、不解析端口以外的输出，
/// 失败就把子进程最后几行原样甩到窗口上，让人能自己判断。
/// </summary>
public sealed class PythonServer : IAsyncDisposable
{
    private const string HealthPath = "/api/health";

    /// <summary>固定首选端口，与 `tagger web` 的默认值一致（见 backend 的 cli.py）。</summary>
    private const int PreferredPort = 27100;

    private readonly StringBuilder _log = new();
    private readonly object _logLock = new();
    private Process? _process;
    private HttpClient? _http;

    public string Root { get; }
    public string PythonPath { get; }
    public int Port { get; private set; }
    public string Url => $"http://127.0.0.1:{Port}/";

    /// <summary>子进程 stdout/stderr 的尾部，出错时直接显示。</summary>
    public string LogTail
    {
        get
        {
            lock (_logLock)
            {
                var text = _log.ToString();
                const int max = 4000;
                return text.Length <= max ? text : text[^max..];
            }
        }
    }

    public event Action<string>? Output;

    private PythonServer(string root, string pythonPath)
    {
        Root = root;
        PythonPath = pythonPath;
    }

    /// <summary>
    /// 先把这个仓库找出来，再确认里面有可用的 Python。
    /// 顺序：显式参数 → 环境变量 TAGGER_ROOT → 从 exe 目录逐级往上找。
    /// </summary>
    public static PythonServer Locate(string? explicitRoot = null)
    {
        var root = ResolveRoot(explicitRoot)
                   ?? throw new ServerStartException(
                       "找不到 tagger 仓库根目录。\n\n" +
                       "把 Tagger.Shell.exe 放在仓库里（例如 dist\\desktop\\），\n" +
                       "或者设置环境变量 TAGGER_ROOT 指向它。");

        var python = ResolvePython(root)
                     ?? throw new EnvNotInitializedException(
                         $"在 {root} 下没找到可用的 Python。\n\n" +
                         "点下面的「初始化环境」装一次就行；也可以自己跑 install.bat，" +
                         "或用 TAGGER_PYTHON 指定解释器路径。");

        return new PythonServer(root, python);
    }

    /// <summary>
    /// 只找仓库根目录，不要求 Python 已经就绪。
    /// 装环境的流程要用它——那会儿恰恰还没有可用的解释器。
    /// </summary>
    public static string? FindRoot() => ResolveRoot(null);

    private static string? ResolveRoot(string? explicitRoot)
    {
        if (!string.IsNullOrWhiteSpace(explicitRoot) && LooksLikeRepo(explicitRoot))
            return Path.GetFullPath(explicitRoot);

        var fromEnv = Environment.GetEnvironmentVariable("TAGGER_ROOT");
        if (!string.IsNullOrWhiteSpace(fromEnv) && LooksLikeRepo(fromEnv))
            return Path.GetFullPath(fromEnv);

        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        for (var i = 0; i < 8 && dir is not null; i++, dir = dir.Parent)
        {
            if (LooksLikeRepo(dir.FullName)) return dir.FullName;
        }
        return null;
    }

    /// <summary>认根目录的标志：包目录和 CLI 入口都在。单看某个文件容易被空文件骗。</summary>
    private static bool LooksLikeRepo(string dir) =>
        File.Exists(Path.Combine(dir, "tagger", "cli.py")) &&
        Directory.Exists(Path.Combine(dir, "tagger", "web"));

    private static string? ResolvePython(string root)
    {
        var fromEnv = Environment.GetEnvironmentVariable("TAGGER_PYTHON");
        if (!string.IsNullOrWhiteSpace(fromEnv) && File.Exists(fromEnv)) return fromEnv;

        // Windows 便携环境优先；其次是本地 venv；再不行交给 PATH。
        string[] candidates =
        [
            Path.Combine(root, "env", "python", "python.exe"),
            Path.Combine(root, ".venv", "Scripts", "python.exe"),
            Path.Combine(root, "env", "linux", "bin", "python"),
            Path.Combine(root, ".venv", "bin", "python"),
        ];
        foreach (var c in candidates)
        {
            if (File.Exists(c)) return c;
        }

        foreach (var name in new[] { "python.exe", "python", "python3" })
        {
            var resolved = SearchPath(name);
            if (resolved is not null) return resolved;
        }
        return null;
    }

    private static string? SearchPath(string exe)
    {
        var path = Environment.GetEnvironmentVariable("PATH") ?? string.Empty;
        foreach (var dir in path.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
        {
            try
            {
                var full = Path.Combine(dir.Trim('"'), exe);
                if (File.Exists(full)) return full;
            }
            catch (ArgumentException)
            {
                // PATH 里混进非法字符，跳过这一段，别让整个启动失败。
            }
        }
        return null;
    }

    /// <summary>
    /// 端口优先固定（与 CLI / run-web.cmd 的默认值一致）。
    ///
    /// 这不只是为了好记：WebView2 的 localStorage / IndexedDB 按 **origin** 隔离，
    /// 而 origin 含端口。以前每次启动都让内核随机分一个端口，等于每次都换了个站点，
    /// 于是主题、上次任务、轮询间隔这些本机偏好在重启后集体失效——只有把端口钉死，
    /// 写下的偏好下次才读得回来。
    ///
    /// 固定端口被占（例如同时开着 run-web.cmd）才退回系统分配：这一次读不到旧偏好，
    /// 但界面照常能用，不会因为端口冲突起不来。
    /// </summary>
    private static int PickPort()
    {
        if (IsPortFree(PreferredPort)) return PreferredPort;
        return PickFreePort();
    }

    private static bool IsPortFree(int port)
    {
        var listener = new TcpListener(IPAddress.Loopback, port);
        try
        {
            listener.Start();
            return true;
        }
        catch (SocketException)
        {
            return false;
        }
        finally
        {
            try { listener.Stop(); } catch (SocketException) { /* 没起来，无需收尾 */ }
        }
    }

    /// <summary>让内核分配一个空闲端口，再关掉监听把端口交还给 uvicorn。</summary>
    private static int PickFreePort()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        try
        {
            return ((IPEndPoint)listener.LocalEndpoint).Port;
        }
        finally
        {
            listener.Stop();
        }
    }

    /// <summary>起子进程并等到 /api/health 真的通。返回时 URL 可用。</summary>
    public async Task StartAsync(CancellationToken ct = default)
    {
        if (_process is not null) throw new InvalidOperationException("服务已经在跑了");

        Port = PickPort();
        var psi = new ProcessStartInfo
        {
            FileName = PythonPath,
            Arguments = $"-m tagger.cli web --host 127.0.0.1 --port {Port} --no-open",
            WorkingDirectory = Root,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        // 子进程的中文输出走 UTF-8，否则 GBK/UTF-8 会打架出乱码。
        psi.Environment["PYTHONUTF8"] = "1";
        psi.Environment["PYTHONIOENCODING"] = "utf-8";
        psi.Environment["PYTHONUNBUFFERED"] = "1";

        var process = new Process { StartInfo = psi, EnableRaisingEvents = true };
        process.OutputDataReceived += (_, e) => Append(e.Data);
        process.ErrorDataReceived += (_, e) => Append(e.Data);

        if (!process.Start())
            throw new ServerStartException("Python 进程没能启动（Start 返回 false）。");

        _process = process;
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();

        _http = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };

        try
        {
            await WaitForHealthAsync(process, ct);
        }
        catch
        {
            await KillAsync();
            throw;
        }
    }

    private async Task WaitForHealthAsync(Process process, CancellationToken ct)
    {
        var deadline = DateTime.UtcNow.AddSeconds(90);
        var health = new Uri(new Uri(Url), HealthPath.TrimStart('/'));

        while (DateTime.UtcNow < deadline)
        {
            ct.ThrowIfCancellationRequested();

            if (process.HasExited)
            {
                throw new ServerStartException(
                    $"本地服务刚启动就退出了（退出码 {process.ExitCode}）。\n\n{LogTail}");
            }

            try
            {
                using var resp = await _http!.GetAsync(health, ct);
                if (resp.IsSuccessStatusCode) return;
            }
            catch (HttpRequestException)
            {
                // 还没起来，正常
            }
            catch (TaskCanceledException) when (!ct.IsCancellationRequested)
            {
                // 单次请求超时，继续等
            }

            await Task.Delay(300, ct);
        }

        throw new ServerStartException(
            $"等了 90 秒，{Url} 仍然没有响应。\n\n" +
            "常见原因：依赖没装（fastapi / uvicorn）、端口被拦、或首次启动被杀软扫描。\n\n" +
            LogTail);
    }

    private void Append(string? line)
    {
        if (string.IsNullOrWhiteSpace(line)) return;
        lock (_logLock)
        {
            _log.AppendLine(line);
            // 长任务会刷很多行，留最后 8KB 就够定位问题了。
            if (_log.Length > 8192) _log.Remove(0, _log.Length - 8192);
        }
        Output?.Invoke(line);
    }

    /// <summary>先请它自己收尾（Kill 掉整棵进程树），失败也不再纠缠。</summary>
    public async Task KillAsync()
    {
        var process = _process;
        _process = null;

        _http?.Dispose();
        _http = null;

        if (process is null) return;

        try
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
                // 给系统一点时间回收句柄；不等的话下次删目录会撞"文件占用"。
                using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
                await process.WaitForExitAsync(cts.Token);
            }
        }
        catch (InvalidOperationException)
        {
            // 已经退了
        }
        catch (OperationCanceledException)
        {
            // 5 秒还没退，放弃等待，反正进程已经被 Kill
        }
        finally
        {
            process.Dispose();
        }
    }

    public async ValueTask DisposeAsync() => await KillAsync();
}

public sealed class ServerStartException(string message) : Exception(message);

/// <summary>
/// 环境还没初始化：缺 env\python，或者依赖没装全。
///
/// 单独一类是为了让界面只在这条分支上给「初始化环境」按钮——端口占用、
/// 服务崩了那种错误点安装只会白等十分钟。
/// </summary>
public sealed class EnvNotInitializedException(string message) : Exception(message);
