from discord import app_commands
from config import OWNER_ID

class OwnerOnly(app_commands.CheckFailure):
    pass


def owner_only():
    def predicate(interaction) -> bool:
        if interaction.user.id != OWNER_ID:
            raise OwnerOnly()
        return True
    return app_commands.check(predicate)