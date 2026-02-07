from .user_handle import UserHandle

__red_end_user_data_statement__ = (
    "This cog stores your user ID and an optional custom role name (handle) per server "
    "to maintain a role that matches your display name or custom handle for tagging."
)


async def setup(bot):
    await bot.add_cog(UserHandle(bot))
