import asyncio
import random
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands, tasks

import mido_utils
from midobot import MidoBot
from models.db import CachedImage, GuildDB


class NSFW(commands.Cog):
    def __init__(self, bot: MidoBot):
        self.bot = bot

        self.api = mido_utils.NSFW_DAPIs(self.bot.http_session, self.bot.db)
        self.reddit = mido_utils.RedditAPI(self.bot.config['reddit_credentials'], self.bot.http_session, self.bot.db)
        self.neko = mido_utils.NekoAPI(session=self.bot.http_session, db=self.bot.db)

        self._cd = commands.CooldownMapping.from_cooldown(rate=2, per=1, type=commands.BucketType.guild)

        self.fill_the_database.start()

        self.active_auto_nsfw_services = list()
        self.bot.loop.create_task(self.start_auto_nsfw_services())
        self.bot.loop.create_task(self.start_checking_urls_in_db())

    async def start_checking_urls_in_db(self):
        await self.bot.wait_until_ready()

        while True:
            images = await CachedImage.get_oldest_checked_images(self.bot, limit=100)
            for image in images:
                if not await image.url_is_working():
                    await image.delete()
                else:
                    await image.url_is_just_checked()
                await asyncio.sleep(0.5)

    async def start_auto_nsfw_services(self):
        auto_nsfw_guilds = await GuildDB.get_auto_nsfw_guilds(bot=self.bot)
        for guild in auto_nsfw_guilds:
            self.add_auto_nsfw_tasks(guild)
            await asyncio.sleep(0.66)

    def add_auto_nsfw_tasks(self, guild: GuildDB, _type=None):
        if (_type is None or _type == 'hentai') and guild.auto_hentai_channel_id:
            task = self.bot.loop.create_task(self.auto_nsfw_loop(guild, _type='hentai'), name=f'{guild.id}_hentai')
            self.active_auto_nsfw_services.append(task)

        if (_type is None or _type == 'porn') and guild.auto_porn_channel_id:
            task = self.bot.loop.create_task(self.auto_nsfw_loop(guild, _type='porn'), name=f'{guild.id}_porn')
            self.active_auto_nsfw_services.append(task)

    def cancel_auto_nsfw_task(self, guild: GuildDB, _type='hentai'):
        for task in self.active_auto_nsfw_services:  # find the guild
            if task.get_name() == f'{guild.id}_{_type}':
                task.cancel()
                self.active_auto_nsfw_services.remove(task)

    async def auto_nsfw_loop(self, guild: GuildDB, _type='hentai'):
        if _type == 'hentai':
            db_channel_id = guild.auto_hentai_channel_id
            db_tags = guild.auto_hentai_tags
            db_interval = guild.auto_hentai_interval
        elif _type == 'porn':
            db_channel_id = guild.auto_porn_channel_id
            db_tags = guild.auto_porn_tags
            db_interval = guild.auto_porn_interval
        else:
            raise Exception("Invalid NSFW type!")

        nsfw_channel = self.bot.get_channel(db_channel_id)

        image_cache: Dict[str, List[str]] = dict()
        fail_counter = 0
        while nsfw_channel and fail_counter < 5:  # if channel isn't found or set, code goes to the end
            time = mido_utils.Time()
            tags = random.choice(db_tags) if db_tags else None

            try:
                image = image_cache[tags].pop()
            except KeyError:
                if _type == 'hentai':
                    image_cache[tags] = [x[0] for x in await self._hentai(tags=tags, limit=25)]
                elif _type == 'porn':
                    image_cache[tags] = [x.url for x in
                                         await self.reddit.get_reddit_post_from_db(
                                             self.bot,
                                             category='porn',
                                             tags=[tags] if tags else None,
                                             limit=50,
                                             allow_gif='gif' in tags if tags else False
                                         )]
                else:
                    raise Exception

                image = image_cache[tags].pop()
            except mido_utils.NotFoundError:
                e = mido_utils.Embed(bot=self.bot,
                                     colour=discord.Colour.red(),
                                     description=f"Could  not find anything with tag: `{tags}`")
                await nsfw_channel.send(embed=e)

                fail_counter += 1
                continue

            self.bot.logger.debug(f"Sent auto-{_type} in {time.passed_seconds_in_float_formatted}:\n"
                                  f"\t\t\tServer\t: {nsfw_channel.guild.name} ({nsfw_channel.guild.id})\n"
                                  f"\t\t\tChannel\t: #{nsfw_channel} ({nsfw_channel.id})\n"
                                  f"\t\t\tTags\t: {tags}")
            try:
                await self.send_nsfw_embed(nsfw_channel, image, auto_nsfw_mode=True)
            except discord.Forbidden:
                nsfw_channel = None  # reset
                break

            await asyncio.sleep(db_interval)

        if not nsfw_channel or fail_counter >= 5:
            return await guild.set_auto_nsfw(type=_type)  # reset

    async def send_nsfw_embed(self, ctx_or_channel, image_url: str, db_obj: CachedImage = None,
                              auto_nsfw_mode: bool = False):
        report_emoji = '📧'
        if db_obj:
            desc = f"Image not working? Report by clicking on {report_emoji} below."
        else:
            desc = f"Image not working? [Click here.]({image_url})"
            if auto_nsfw_mode:
                desc += '\n*Image reporting is disabled in auto-NSFW mode.*'

        e = mido_utils.Embed(bot=self.bot, image_url=image_url, description=desc)
        e.set_footer(text=f"{self.bot.name.title()} NSFW API")
        m = await ctx_or_channel.send(embed=e)

        if db_obj:
            await m.add_reaction(report_emoji)

            reaction = await e.wait_for_reaction(bot=self.bot, message=m, emotes_to_wait=[report_emoji])
            if reaction:
                await db_obj.report()
                e.description = "We've got your report. Thank you."
                await m.edit(embed=e)

    async def _hentai(self, tags: str, limit=1, allow_video=False) -> List[Tuple[str, Optional[CachedImage]]]:
        if not tags:
            return [(x.url, x)
                    for x in await self.reddit.get_reddit_post_from_db(self.bot, category='hentai', limit=limit)]
        else:
            return [(x, None)
                    for x in await self.api.get_bomb(tags, limit, allow_video)]

    @tasks.loop(hours=1.0)
    async def fill_the_database(self):
        self.bot.logger.info('Checking hot posts from Reddit...')
        await self.reddit.fill_the_database()
        self.bot.logger.info('Checking hot posts from Reddit is done.')

    @fill_the_database.error
    async def task_error(self, error):
        await self.bot.get_cog('ErrorHandling').on_error("An error occurred while filling the database.")

    @fill_the_database.before_loop
    async def before_task(self):
        await self.bot.wait_until_ready()

    async def cog_check(self, ctx: mido_utils.Context):
        bucket = self._cd.get_bucket(ctx.message)
        retry_after = bucket.update_rate_limit()
        if retry_after:  # if on cooldown
            raise commands.CommandOnCooldown(bucket, retry_after)

        if not isinstance(ctx.channel, discord.DMChannel) and not ctx.channel.is_nsfw():
            raise commands.NSFWChannelRequired(ctx.channel)

        return True

    def cog_unload(self):
        self.fill_the_database.cancel()

        for task in self.active_auto_nsfw_services:
            task.cancel()

        self.active_auto_nsfw_services = list()

    @commands.command(aliases=['boob'])
    async def boobs(self, ctx: mido_utils.Context):
        """Get a random boob picture."""

        image = (await self.reddit.get_reddit_post_from_db(ctx.bot, category='porn', tags=['boobs']))[0]
        ctx.bot.loop.create_task(self.send_nsfw_embed(ctx, image.url, image))

    @commands.command(aliases=['butt', 'ass'])
    async def butts(self, ctx: mido_utils.Context):
        """Get a random butt picture."""

        image = (await self.reddit.get_reddit_post_from_db(ctx.bot, category='porn', tags=['butts']))[0]
        ctx.bot.loop.create_task(self.send_nsfw_embed(ctx, image.url, image))

    @commands.command()
    async def porn(self, ctx: mido_utils.Context, *, tag: str = None):
        """Get a random porn content. A tag can be provided."""

        image = (await self.reddit.get_reddit_post_from_db(ctx.bot, category='porn', tags=[tag] if tag else None))[0]
        ctx.bot.loop.create_task(self.send_nsfw_embed(ctx, image.url, image))

    @commands.command()
    async def pussy(self, ctx: mido_utils.Context):
        """Get a random pussy image."""

        image = (await self.reddit.get_reddit_post_from_db(ctx.bot, category='porn', tags=['pussy']))[0]
        ctx.bot.loop.create_task(self.send_nsfw_embed(ctx, image.url, image))

    @commands.command()
    async def asian(self, ctx: mido_utils.Context):
        """Get a random asian porn content."""

        image = (await self.reddit.get_reddit_post_from_db(ctx.bot, category='porn', tags=['asian']))[0]
        ctx.bot.loop.create_task(self.send_nsfw_embed(ctx, image.url, image))

    @commands.command()
    async def gelbooru(self, ctx: mido_utils.Context, *, tags: str = None):
        """Get a random image from Gelbooru.

        You must put '+' between different tags.
        `{0.prefix}hentaibomb yuri+group`"""

        image = await self.api.get('gelbooru', tags)
        ctx.bot.loop.create_task(self.send_nsfw_embed(ctx, image[0]))

    @commands.command()
    async def rule34(self, ctx: mido_utils.Context, *, tags: str = None):
        """Get a random image from Rule34.

        You must put '+' between different tags.
        `{0.prefix}hentaibomb yuri+group`"""

        image = await self.api.get('rule34', tags)
        ctx.bot.loop.create_task(self.send_nsfw_embed(ctx, image[0]))

    @commands.command()
    async def danbooru(self, ctx: mido_utils.Context, *, tags: str = None):
        """Get a random image from Danbooru.

        You must put '+' between different tags.
        `{0.prefix}hentaibomb yuri+group`

        **Danbooru doesn't allow more than 2 tags.**"""
        try:
            image = await self.api.get('danbooru', tags)
        except commands.TooManyArguments:
            raise commands.TooManyArguments("Danbooru doesn't allow more than 2 tags.")

        ctx.bot.loop.create_task(self.send_nsfw_embed(ctx, image[0]))

    @commands.command(name='lewdneko')
    async def lewd_neko(self, ctx: mido_utils.Context):
        """Get a random lewd neko image."""

        image = await self.neko.get_random_neko(nsfw=True)
        ctx.bot.loop.create_task(self.send_nsfw_embed(ctx, image.url))

    @commands.command()
    async def hentai(self, ctx: mido_utils.Context, *, tags: str = None):
        """Get a random hentai image.

        You must put '+' between different tags.
        `{0.prefix}hentaibomb yuri+group`"""
        image, db_obj = (await self._hentai(tags, limit=1))[0]
        ctx.bot.loop.create_task(self.send_nsfw_embed(ctx, image, db_obj))

    @commands.command(name='hentaibomb')
    async def hentai_bomb(self, ctx: mido_utils.Context, *, tags: str = None):
        """Get multiple hentai images.

        You must put '+' between different tags.
        `{0.prefix}hentaibomb yuri+group`"""
        image_and_db_objs = await self._hentai(tags, limit=3, allow_video=True)

        await ctx.send(content="\n".join(im[0] for im in image_and_db_objs))

    @commands.has_permissions(manage_messages=True)
    @commands.command(name='autohentai')
    async def auto_hentai(self, ctx: mido_utils.Context, interval: mido_utils.Int32() = None, *, tags: str = None):
        """Have hentai automatically posted!

        Interval argument can be 5 seconds minimum.

        Put `+` between tags.
        Put `|` between tag groups. A random tag group will be chosen each time.
        (Tag argument can be left empty.)

        Don't type any argument to disable the autohentai service.

        Only 1 autohentai service can be active in a server.
        You need Manage Messages permission to use this command."""

        if not interval:
            if not ctx.guild_db.auto_hentai_channel_id:  # if already disabled
                raise commands.BadArgument("Autohentai is already disabled.")

            else:
                self.cancel_auto_nsfw_task(guild=ctx.guild_db, _type='hentai')
                await ctx.guild_db.set_auto_nsfw(type='hentai')  # disable

                return await ctx.send_success("Autohentai service has successfully been disabled.")

        if interval < 5:
            raise commands.UserInputError("Interval can not be less than 5!")

        await ctx.guild_db.set_auto_nsfw(type='hentai',
                                         channel_id=ctx.channel.id,
                                         tags=tags.split('|') if tags else None,
                                         interval=interval)

        self.cancel_auto_nsfw_task(guild=ctx.guild_db, _type='hentai')
        self.add_auto_nsfw_tasks(guild=ctx.guild_db, _type='hentai')

        return await ctx.send_success(f"Success! I'll post hentai in this channel "
                                      f"every **{mido_utils.Time.parse_seconds_to_str(interval)}** "
                                      f"with these tags: `{tags if tags else 'random'}`")

    @commands.has_permissions(manage_messages=True)
    @commands.command(name='autoporn')
    async def auto_porn(self, ctx: mido_utils.Context, interval: mido_utils.Int32() = None, *, tags: str = None):
        """Have porn automatically posted!

        Interval argument can be 5 seconds minimum.

        Put `|` between tag groups. A random tag group will be chosen each time.
        Please provide a single tag for each tag group (unlike `autohentai`)
        (Tag argument can be left empty.)

        Don't type any argument to disable the autoporn service.

        Only 1 autoporn service can be active in a server.
        You need Manage Messages permission to use this command."""

        if not interval:
            if not ctx.guild_db.auto_porn_channel_id:  # if already disabled
                raise commands.BadArgument("Autoporn is already disabled.")

            else:
                self.cancel_auto_nsfw_task(guild=ctx.guild_db, _type='porn')
                await ctx.guild_db.set_auto_nsfw(type='porn')  # disable

                return await ctx.send_success("Autoporn service has successfully been disabled.")

        if interval < 5:
            raise commands.UserInputError("Interval can not be less than 5!")

        await ctx.guild_db.set_auto_nsfw(type='porn',
                                         channel_id=ctx.channel.id,
                                         tags=tags.split('|') if tags else None,
                                         interval=interval)

        self.cancel_auto_nsfw_task(guild=ctx.guild_db, _type='porn')
        self.add_auto_nsfw_tasks(guild=ctx.guild_db, _type='porn')

        return await ctx.send_success(f"Success! I'll post porn in this channel "
                                      f"every **{mido_utils.Time.parse_seconds_to_str(interval)}** "
                                      f"with these tags: `{tags if tags else 'random'}`")


def setup(bot):
    bot.add_cog(NSFW(bot))
