import sys, os

# === Config ===
script_dir = os.path.dirname(os.path.abspath(__file__))
env_file = os.path.join(script_dir, '.env')
src_dir = os.path.join(script_dir, 'src')
cli_module = os.path.join(src_dir, 'weread2notion', 'cli.py')

# Bypass proxy
os.environ['NO_PROXY'] = '*'
os.environ['HTTP_PROXY'] = ''
os.environ['HTTPS_PROXY'] = ''


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
    print('[4/4] Sync completed! Check your Notion.')
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
