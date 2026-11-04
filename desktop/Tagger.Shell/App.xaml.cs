using System.Windows;
using System.Windows.Threading;
using Tagger.Shell.Services;

namespace Tagger.Shell;

public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        // 必须在 base.OnStartup 之前：base 会按 StartupUri 把 MainWindow 建出来，
        // 运行时版本不够时窗口只会白屏或直接崩，看不出原因。
        if (!RuntimeCheck.EnsureDotNet())
        {
            Shutdown(1);
            return;
        }

        // 没处理掉的异常会静默退出，双击 exe 的用户只会看到"什么都没发生"。
        DispatcherUnhandledException += OnDispatcherUnhandledException;
        base.OnStartup(e);
    }

    private void OnDispatcherUnhandledException(object sender, DispatcherUnhandledExceptionEventArgs e)
    {
        MessageBox.Show(
            $"界面线程出错了：\n\n{e.Exception.Message}\n\n详情写进了 %LOCALAPPDATA%\\Tagger\\shell.log",
            "Tagger", MessageBoxButton.OK, MessageBoxImage.Error);
        AppLog.Write(e.Exception);
        e.Handled = true;
    }
}
