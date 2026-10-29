"""Connect a tenant's ElevenLabs agent to this API.

Usage:
  python manage.py voice_elevenlabs_sync <username> --base-url https://api.example.com
  python manage.py voice_elevenlabs_sync <username> --base-url ... --agent-id agent_xxx
  python manage.py voice_elevenlabs_sync <username> ... --persona male --voice-id <id> --assistant-name محمود

Needs ELEVENLABS_API_KEY. Without --agent-id, uses the agent already linked to
the tenant, or the workspace's only agent. Issues a new tool token (the old one
stops working), creates or updates the get_menu / search_info / quote_order /
create_order tools, and sets the agent's prompt, greeting, language and voice.
--persona, --voice-id and --assistant-name are saved on the tenant, so later runs
keep them. Safe to re-run, e.g. after the public URL changes.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from knowledge.models import ChatbotConfig
from voice.models import VoiceAgent
from voice.services import elevenlabs


class Command(BaseCommand):
    help = "Create/update a tenant's ElevenLabs agent tools and prompt."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--base-url", required=True, help="Public https URL of this API.")
        parser.add_argument("--agent-id", default="", help="ElevenLabs agent id.")
        parser.add_argument(
            "--persona", choices=VoiceAgent.Persona.values, help="Assistant's gender; match the voice."
        )
        parser.add_argument("--voice-id", help="ElevenLabs voice id (one voice for the whole call).")
        parser.add_argument("--assistant-name", help="Name the assistant introduces itself with.")

    def handle(self, *args, **options):
        base_url = options["base_url"].rstrip("/")
        if not base_url.startswith("https://"):
            raise CommandError("--base-url must be an https URL ElevenLabs can reach.")
        user = get_user_model().objects.filter(username=options["username"]).first()
        if user is None:
            raise CommandError(f"No tenant named {options['username']!r}.")
        agent, _ = VoiceAgent.objects.get_or_create(tenant=user)
        if options["persona"]:
            agent.persona = options["persona"]
        if options["voice_id"] is not None:
            agent.voice_id = options["voice_id"].strip()
        agent.save()
        if options["assistant_name"]:
            cfg, _ = ChatbotConfig.objects.get_or_create(user=user)
            cfg.assistant_name = options["assistant_name"]
            cfg.save()

        try:
            agent_id = options["agent_id"] or agent.agent_id or self._only_agent()
            if VoiceAgent.objects.filter(agent_id=agent_id).exclude(pk=agent.pk).exists():
                raise CommandError(f"Agent {agent_id} is already linked to another tenant.")
            token = agent.issue_token()
            tools = elevenlabs.sync_agent(agent_id, voice_agent=agent, base_url=base_url, token=token)
        except elevenlabs.ElevenLabsError as exc:
            raise CommandError(str(exc)) from exc

        agent.agent_id = agent_id
        agent.save(update_fields=["agent_id", "updated_at"])
        for name, tool_id in tools.items():
            self.stdout.write(f"  {name}: {tool_id}")
        self.stdout.write(self.style.SUCCESS(f"Agent {agent_id} now uses {base_url}"))

    def _only_agent(self) -> str:
        agents = elevenlabs.list_agents()
        if len(agents) != 1:
            names = ", ".join(f"{a.get('name')} ({a.get('agent_id')})" for a in agents) or "none"
            raise CommandError(f"Pass --agent-id; the workspace has {len(agents)} agents: {names}")
        return agents[0]["agent_id"]
