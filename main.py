"""
Точка входа для GitHub Actions.
Вызывает парсеры по очереди. Падение одного не мешает другому.
"""
import logging
import sys
import traceback
import asyncio

logging.basicConfig(
    stream=sys.stdout, level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
)


def run_safely(name, fn):
    logging.info(f'▶️ {name}: старт')
    try:
        fn()
        logging.info(f'✅ {name}: готово')
    except Exception as e:
        logging.error(f'❌ {name}: упал — {e}')
        traceback.print_exc()


def main():
    # 1. Telegram
    try:
        from telegram_parser import run_telegram
        run_safely('telegram_parser', lambda: asyncio.run(run_telegram()))
    except Exception as e:
        logging.error(f'❌ telegram_parser: не удалось импортировать/запустить — {e}')
        traceback.print_exc()

    # 2. Shield
    try:
        from shield_parser import run_shield
        run_safely('shield_parser', lambda: run_shield(force_refresh=False, full=False))
    except Exception as e:
        logging.error(f'❌ shield_parser: не удалось импортировать/запустить — {e}')
        traceback.print_exc()


if __name__ == '__main__':
    main()
