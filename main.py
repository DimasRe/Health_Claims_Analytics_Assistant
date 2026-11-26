import asyncio
import logging
import os

from ai.qwen_mcp_client import BPJSChatbot


EXIT_COMMANDS = ("exit", "keluar", "quit")
RESET_COMMANDS = ("reset", "baru", "/reset")


def configure_logging():
    # Set LOG_LEVEL=WARNING untuk menyembunyikan log pemanggilan MCP tool.
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(message)s",
    )
    # Sembunyikan log teknis permintaan HTTP ke Ollama.
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def main():

    configure_logging()

    print("=" * 55)
    print("       BPJS ANALYTICS CHATBOT")
    print("=" * 55)
    print("Menghubungkan chatbot ke MCP Server...")

    try:
        async with BPJSChatbot() as chatbot:

            print("\nChatbot siap digunakan.")
            print(f"Model: {chatbot.model}")
            print("Tools tersedia:")
            for name in chatbot.available_tools:
                print("-", name)
            print("Ketik 'reset' untuk memulai percakapan baru.")
            print("Ketik 'exit' atau 'keluar' untuk mengakhiri.")
            print("-" * 55)

            while True:

                try:
                    question = input("\nAnda: ").strip()

                except (EOFError, KeyboardInterrupt):
                    print("\nMenutup chatbot...")
                    break

                if not question:
                    continue

                if question.lower() in EXIT_COMMANDS:
                    print("\nTerima kasih. Chatbot ditutup.")
                    break

                if question.lower() in RESET_COMMANDS:
                    chatbot.reset_conversation()
                    print("\nPercakapan baru dimulai.")
                    continue

                try:
                    answer = await chatbot.ask(question)

                    print("\nBPJS Assistant:")
                    print(answer)

                except Exception as error:
                    print("\nTerjadi kesalahan saat memproses pertanyaan.")
                    print("Detail:", error)

    except Exception as error:
        print("\nGagal menghubungkan chatbot.")
        print("Detail:", error)


if __name__ == "__main__":
    asyncio.run(main())
