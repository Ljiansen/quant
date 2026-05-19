"""V4策略仪表盘 - 启动脚本"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dashboard.app import create_app

if __name__ == '__main__':
    app = create_app()
    print("=" * 50)
    print("V4策略仪表盘")
    print("访问地址: http://localhost:8088")
    print("=" * 50)
    app.run(host='0.0.0.0', port=8088, debug=False)
