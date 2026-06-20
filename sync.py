import sys, os, socket

# === Config ===
script_dir = os.path.dirname(os.path.abspath(__file__))
env_file = os.path.join(script_dir, '.env')
src_dir = os.path.join(script_dir, 'src')
cli_module = os.path.join(src_dir, 'weread2notion', 'cli.py')

# Smart proxy detection: use proxy if available, bypass if not
def detect_proxy(host='127.0.0.1', port=7890, timeout=1):
    """Check if local proxy is running."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        s.close()
        return True
    except (socket.error, OSError):
        return False

if detect_proxy():
    proxy_url = 'http://127.0.0.1:7890'
    os.environ['HTTP_PROXY'] = proxy_url
    os.environ['HTTPS_PROXY'] = proxy_url
    os.environ['http_proxy'] = proxy_url
    os.environ['https_proxy'] = proxy_url
    # Clear NO_PROXY so requests actually uses the proxy
    os.environ.pop('NO_PROXY', None)
    os.environ.pop('no_proxy', None)
    proxy_status = 'ON (127.0.0.1:7890)'
else:
    os.environ['NO_PROXY'] = '*'
    os.environ['HTTP_PROXY'] = ''
    os.environ['HTTPS_PROXY'] = ''
    proxy_status = 'OFF (direct connection)'


def wait():
    try:
        input('Press Enter to exit...')
    except EOFError:
        pass


print('')
print('WeRead2Notion - Sync WeRead notes to Notion')
print('=' * 44)
print('')

# Check Python
print('[1/4] Checking environment...')
print('  [OK] Python {}'.format(sys.version.split()[0]))
print('  [OK] Proxy: {}'.format(proxy_status))

# Check project files
if not os.path.isfile(cli_module):
    print('  [FAIL] Project source files not found')
    print('  Missing: {}'.format(cli_module))
    wait()
    sys.exit(1)
print('  [OK] Project source files found')

# Check .env
if not os.path.isfile(env_file):
    print('  [FAIL] .env config file not found')
    print('  Please create .env file in: {}'.format(script_dir))
    print('  Format:')
    print('    WEREAD_API_KEY=your_key')
    print('    NOTION_TOKEN=your_token')
    print('    NOTION_PAGE=your_page_id')
    wait()
    sys.exit(1)
print('  [OK] .env config file found')

# Load .env
print('')
print('[2/4] Loading credentials...')
with open(env_file, 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            os.environ[key.strip()] = value.strip()
            print('  Loaded: {}'.format(key.strip()))

# Run sync
print('')
print('[3/4] Syncing...')
print('-' * 44)

sys.path.insert(0, src_dir)
try:
    from weread2notion.cli import main
    main(['sync'])
    print('-' * 44)
    print('')
    print('[4/4] Sync completed!')
    print('  - Books synced to Notion')
    print('  - Personal Library dashboard updated')
    print('  Check your Notion workspace!')
except SystemExit as e:
    if e.code != 0:
        print('-' * 44)
        print('')
        print('[4/4] Sync failed. Check errors above.')
except Exception as e:
    print('-' * 44)
    print('')
    print('[4/4] Sync failed: {}'.format(e))

print('')
wait()
