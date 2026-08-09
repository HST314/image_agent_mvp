"""CLI entrypoint for recoverable image projects."""
from workspace_cli import main
from configs.env_loader import load_dotenv  # 引入 .env 加载器

load_dotenv(".env")  # 在程序启动时自动读取当前目录下的 .env 文件

if __name__ == "__main__":
    raise SystemExit(main())
