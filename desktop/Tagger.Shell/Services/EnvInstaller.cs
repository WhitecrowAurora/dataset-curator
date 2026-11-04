using System.Diagnostics;
using System.IO;
using System.Text;

namespace Tagger.Shell.Services;

/// <summary>
/// 跑一次 install.bat，把它的输出实时喂给界面。
///
/// 为什么不在 Shell 里自己 pip install：
///   装什么、钉什么版本、要不要写 ._pth、离线时从哪个目录取轮子，
///   全在 setup_embedded_env.ps1 里。Shell 再实现一遍必然和它漂移——
///   到时候"点按钮装出来的环境"和"跑 install.bat 装出来的环境"不一样，
///   是最难查的那类问题。
///
/// 为什么用 cmd 而不是直接起 powershell：
///   install.bat 是文档里写的唯一入口，用户可能在里面加过镜像/离线参数。
///   走它 = 和手动装完全同一条路径。
/// </summary>
public sealed class EnvInstaller
{
    private const int TailLimit = 4000;

    private readonly StringBuilder _tail = new();
    private readonly object _lock = new();

    /// <summary>逐行输出。可能在后台线程触发，界面侧自己切 Dispatcher。</summary>
    public event Action<string>? Output;

    /// <summary>阶段变了（解压 / 装依赖 / 自检）。只在识别到阶段行时触发。</summary>
    public event Action<string>? StageChanged;

    public string LogTail
    {
        get
        {
            lock (_lock)
            {
                var text = _tail.ToString();
                return text.Length <= TailLimit ? text : text[^TailLimit..];
            }
        }
    }

    /// <summary>install.bat 里的阶段行 → 人话。按顺序匹配，先命中的算。</summary>
    private static readonly (string Key, string Label)[] Stages =
    [
        ("[env] 解压", "解压 Python 运行时"),
        ("[env] 引导 pip", "准备 pip"),
        ("[env] 安装 requirements", "下载并安装依赖（最慢的一步，可能要几分钟）"),
        ("[env] NL 引擎", "检查 NL 引擎"),
        ("[env] 自检", "自检"),
        ("[env] 完成", "完成"),
    ];

    public async Task<bool> RunAsync(string repoRoot, CancellationToken ct = default)
    {
        var bat = Path.Combine(repoRoot, "install.bat");
        if (!File.Exists(bat))
        {
            Emit($"找不到 {bat}，这个包可能不完整。");
            return false;
        }

        // 用完整路径而不是裸 "cmd.exe"：
        //   PATH 被污染时（某些工具目录里也放一份 cmd.exe），裸名字会命中
        //   别人的副本，install.bat 就在一个奇怪的 shell 里跑。
        var shell = Path.Combine(Environment.SystemDirectory, "cmd.exe");
        if (!File.Exists(shell))
            shell = Environment.GetEnvironmentVariable("ComSpec") ?? "cmd.exe";

        var psi = new ProcessStartInfo(shell)
        {
            WorkingDirectory = repoRoot,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            // chcp 65001 之后再解码成 UTF-8：PowerShell 的 Write-Host 会按
            // 控制台代码页写字节，不切代码页的话中文全是乱码。
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        psi.ArgumentList.Add("/d");   // 跳过 AutoRun：有些机器那里挂着一个坏路径
        psi.ArgumentList.Add("/c");
        // 用单个 & 而不是 &&：chcp 万一被策略拦掉，安装也得照跑
        // （那只会让日志里的中文变成乱码，不影响装出来的环境）。
        psi.ArgumentList.Add("chcp 65001 >nul & call install.bat");

        using var process = new Process { StartInfo = psi, EnableRaisingEvents = true };
        process.OutputDataReceived += (_, e) => Emit(e.Data);
        process.ErrorDataReceived += (_, e) => Emit(e.Data);

        if (!process.Start())
        {
            Emit("cmd.exe 没能启动。");
            return false;
        }
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();

        try
        {
            await process.WaitForExitAsync(ct);
        }
        catch (OperationCanceledException)
        {
            try { process.Kill(entireProcessTree: true); } catch (Exception) { /* 已退出 */ }
            return false;
        }

        if (process.ExitCode != 0)
            Emit($"[安装失败] install.bat 退出码 {process.ExitCode}。");
        return process.ExitCode == 0;
    }

    private void Emit(string? line)
    {
        if (string.IsNullOrWhiteSpace(line)) return;

        lock (_lock)
        {
            _tail.AppendLine(line);
        }

        foreach (var (key, label) in Stages)
        {
            if (line.Contains(key, StringComparison.Ordinal))
            {
                StageChanged?.Invoke(label);
                break;
            }
        }

        Output?.Invoke(line);
    }
}
