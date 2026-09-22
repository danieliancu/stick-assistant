import logging
import sys

from django.core.management.base import BaseCommand

from assistant.services.orchestrator import Orchestrator
from assistant.services.session import ConversationSession

logger = logging.getLogger("assistant.chat")

EXIT_WORDS = {"exit", "quit", "iesire", "ieșire", "gata"}


class Command(BaseCommand):
    help = "Start an interactive terminal chat with the personal AI assistant."

    def handle(self, *args, **options):
        for stream in (sys.stdout, sys.stdin):
            if hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8")
                except (ValueError, OSError):
                    pass

        orchestrator = Orchestrator()
        session = ConversationSession()

        self.stdout.write("Personal AI Assistant")
        self.stdout.write("Type 'exit' to quit.\n")

        while True:
            try:
                message = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                self.stdout.write("")
                break

            if not message:
                continue
            if message.lower() in EXIT_WORDS:
                break

            try:
                reply = orchestrator.handle_message(session, message)
            except KeyboardInterrupt:
                self.stdout.write("\n(interrupted)")
                continue
            except Exception:  # keep the session alive; details go to the log only
                logger.exception("Unexpected error while handling a message")
                self.stdout.write(self.style.ERROR("Assistant: An unexpected error occurred."))
                continue

            style = self.style.SUCCESS if reply.ok else self.style.WARNING
            self.stdout.write(style("Assistant: ") + reply.text + "\n")

        self.stdout.write("Goodbye!")
