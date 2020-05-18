import asyncio
import audioop
import functools
import itertools
import math
import random

import discord
import youtube_dl
from async_timeout import timeout
from discord.ext import commands

from main import MidoBot
from services import menu_stuff, context


class VoiceError(Exception):
    pass


class YTDLError(Exception):
    pass


class AlreadyConnected(Exception):
    pass


class MusicError(Exception):
    pass


class YTDLSource(discord.PCMVolumeTransformer):
    YTDL_OPTIONS = {
        'format': 'bestaudio/best',
        'extractaudio': True,
        'audioformat': 'mp3',
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames': True,
        'playlistend': 100,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0',
        'cachedir': False
    }

    FFMPEG_OPTIONS = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options': '-vn',
    }

    ytdl = youtube_dl.YoutubeDL(YTDL_OPTIONS)

    def __init__(self, ctx: context.Context, source: discord.FFmpegPCMAudio, *, data: dict, volume: float = 0.1):
        super().__init__(source, volume)

        self.requester = ctx.author
        self.channel = ctx.channel
        self.data = data
        self.ctx = ctx

        self.uploader = data.get('uploader')
        self.uploader_url = data.get('uploader_url')
        date = data.get('upload_date')
        self.upload_date = date[6:8] + '.' + date[4:6] + '.' + date[0:4]
        self.title = data.get('title')
        self.thumbnail = data.get('thumbnail')
        self.description = data.get('description')
        self.duration = self.parse_duration(int(data.get('duration')))
        self.tags = data.get('tags')
        self.url = data.get('webpage_url')
        self.views = data.get('view_count')
        self.likes = data.get('like_count')
        self.dislikes = data.get('dislike_count')
        self.stream_url = data.get('url')
        self._played_duration = 0

    def __str__(self):
        return '**{0.title}** by **{0.uploader}**'.format(self)

    @classmethod
    async def create_source(cls, ctx: context.Context, search: str, *, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()

        partial = functools.partial(cls.ytdl.extract_info, url=search, download=False, process=True)
        processed_info = await loop.run_in_executor(None, partial)

        if processed_info is None:
            raise YTDLError('Couldn\'t find anything that matches `{}`'.format(search))

        # if we have a list of entries (most likely a playlist or a search)
        if 'entries' in processed_info:
            return [cls(ctx, discord.FFmpegPCMAudio(song['url'], **cls.FFMPEG_OPTIONS), data=song)
                    for song in processed_info['entries']]
        # if a song link is provided
        else:
            return [cls(ctx, discord.FFmpegPCMAudio(processed_info['url'], **cls.FFMPEG_OPTIONS), data=processed_info)]

    @staticmethod
    def parse_duration(duration: int, short: bool = True) -> str:
        duration = int(duration)
        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)

        duration = ""
        if not short:
            if days > 0:
                duration += '{} days\n'.format(days)
            if hours > 0:
                duration += '{} hours\n'.format(hours)
            if minutes > 0:
                duration += '{} minutes\n'.format(minutes)
            if seconds > 0:
                duration += '{} seconds\n'.format(seconds)
        else:
            if days > 0:
                duration += f'{days:02d}:'
            if hours > 0:
                duration += f'{hours:02d}:'

            duration += f'{minutes:02d}:{seconds:02d}'

        return duration

    @property
    def played_duration(self) -> str:
        return self.parse_duration(self._played_duration)

    def read(self):
        self._played_duration += 0.02
        ret = self.original.read()
        return audioop.mul(ret, 2, min(self._volume, 2.0))


class Song:
    __slots__ = ('ctx', 'source', 'requester')

    def __init__(self, source: YTDLSource):
        self.ctx = source.ctx
        self.source = source
        self.requester = source.requester

    def create_embed(self):
        e = discord.Embed(description='```css\n{0.source.title}\n```'.format(self),
                          color=0x15a34a)
        e.set_author(
            icon_url="https://cdn.discordapp.com/attachments/244405453948321792/707797956295655434/PngItem_2087614.png",
            name="Now Playing",
            url=self.source.url)
        e.add_field(name='Duration', value=f"{self.source.played_duration}/{self.source.duration}")
        e.add_field(name='Requester', value=self.requester.mention)
        e.add_field(name='Uploader', value='[{0.source.uploader}]({0.source.uploader_url})'.format(self))
        e.add_field(name="Upload Date", value=self.source.upload_date)
        e.add_field(name="View Count", value='{:,}'.format(self.source.views))

        if self.source.likes and self.source.dislikes:
            likes = self.source.likes
            dislikes = self.source.dislikes
            e.add_field(name="Like/Dislike Count",
                        value="{:,}/{:,}\n(**{:.2f}%**)".format(likes, dislikes, (likes * 100 / (likes + dislikes))))

        # e.set_footer(icon_url=self.ctx.guild.icon_url,
        #              text=f"{self.ctx.guild.name} | Volume: {int(self.source.volume * 100)}%")
        e.set_footer(text=f"Volume: {int(self.source.volume * 100)}%",
                     icon_url="https://i.imgur.com/T0532pn.png")
        e.set_thumbnail(url=self.source.thumbnail)

        return e


class SongQueue(asyncio.Queue):
    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(itertools.islice(self._queue, item.start, item.stop, item.step))
        else:
            return self._queue[item]

    def __iter__(self):
        return self._queue.__iter__()

    def __len__(self):
        return self.qsize()

    def clear(self):
        self._queue.clear()

    def shuffle(self):
        random.shuffle(self._queue)

    def remove(self, index: int):
        del self._queue[index]


class VoiceState:
    def __init__(self, bot: MidoBot):
        self.bot = bot

        self.current = None
        self.voice: discord.VoiceClient = None
        self.exists = True
        self.next = asyncio.Event()
        self.songs = SongQueue()

        self._loop = False
        self._volume = 0.1
        self.skip_votes = []

        self.played_duration = 0
        self.audio_player = bot.loop.create_task(self.audio_player_task())

    def __del__(self):
        self.audio_player.cancel()

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, value: bool):
        self._loop = value

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value: float):
        self._volume = self.current.source.volume = value

    @property
    def is_playing(self):
        return self.voice and self.current

    async def audio_player_task(self):
        while True:
            self.next.clear()
            self.current = None

            if not self.loop:
                try:
                    async with timeout(60):
                        self.current = await self.songs.get()
                except asyncio.TimeoutError:
                    self.bot.loop.create_task(self.stop())
                    return

            self.current.source.volume = self._volume
            self.voice.play(self.current.source, after=self.play_next_song)
            await self.current.source.channel.send(embed=self.current.create_embed())

            await self.next.wait()

    def play_next_song(self, error=None):
        if error:
            raise VoiceError(str(error))
        self.skip_votes.clear()
        self.next.set()

    def skip(self):
        self.skip_votes.clear()

        if self.is_playing:
            self.voice.stop()

    async def stop(self):
        self.songs.clear()

        if self.voice:
            await self.voice.disconnect(force=True)
            self.voice = None


class Music(commands.Cog):
    def __init__(self, bot: MidoBot):
        self.bot = bot

        self.voice_states = {}

    def get_voice_state(self, guild_id: int) -> VoiceState:
        state = self.voice_states.get(guild_id)

        if not state or not state.exists:
            state = VoiceState(self.bot)
            self.voice_states[guild_id] = state

        return state

    async def cog_check(self, ctx: context.Context):
        if not ctx.guild:
            raise commands.NoPrivateMessage('This command can\'t be used in DM channels.')

        return True

    def cog_unload(self):
        for state in self.voice_states.values():
            self.bot.loop.create_task(state.stop())

    async def cog_before_invoke(self, ctx: context.Context):
        ctx.voice_state = self.get_voice_state(ctx.guild.id)

    @commands.command(name='connect')
    async def _join(self, ctx: context.Context):
        """Make me join a voice channel."""
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise MusicError('You are not connected to any voice channel.')

        if ctx.voice_client:
            if ctx.voice_client.channel != ctx.author.voice.channel:
                raise MusicError('Bot is already in a voice channel.')

        destination = ctx.author.voice.channel
        if ctx.voice_state.voice:
            return await ctx.voice_state.voice.move_to(destination)

        ctx.voice_state.voice = await destination.connect()
        await ctx.message.add_reaction('👍')

    @commands.command(name='disconnect', aliases=['destroy', 'd'])
    async def _leave(self, ctx: context.Context):
        """Make me leave the voice channel."""
        if ctx.voice_client:
            await ctx.voice_client.disconnect(force=True)
            try:
                await ctx.voice_state.stop()
                del self.voice_states[ctx.guild.id]
            except Exception as e:
                print(e)

            await ctx.send("I've successfully left the voice channel.")

        else:
            return await ctx.send("I'm not currently not in a voice channel! (or am I 🤔)")

    @commands.command(name='volume', aliases=['v'])
    async def _volume(self, ctx: context.Context, volume: int = None):
        """Change or see the volume."""
        if not ctx.voice_state.is_playing:
            return await ctx.send('Nothing is being played at the moment.')

        if not volume:
            return await ctx.send(f'Current volume: **{int(ctx.voice_state.volume * 100)}**%')

        if 0 > volume > 100:
            return await ctx.send('Volume must be between 0 and 100!')

        ctx.voice_state.volume = volume / 100
        await ctx.send(f'Volume is set to **{volume}**%')

    @commands.command(name='now', aliases=['current', 'playing', 'nowplaying', 'np'])
    async def _now(self, ctx: context.Context):
        """See what's currently playing."""
        if ctx.voice_state.current:
            await ctx.send(embed=ctx.voice_state.current.create_embed())
        else:
            await ctx.send("I'm not currently playing any music!")

    @commands.command(name='pause', aliases=['p'])
    async def _pause(self, ctx: context.Context):
        """Pause the song."""
        if ctx.voice_state.voice.is_paused():
            await ctx.send(f"It's already paused! Use `{ctx.prefix}resume` to resume.")

        elif ctx.voice_state.is_playing and ctx.voice_state.voice.is_playing():
            ctx.voice_state.voice.pause()
            await ctx.message.add_reaction('⏯')

        else:
            await ctx.send("I'm not currently playing any music!")

    @commands.command(name='resume')
    async def _resume(self, ctx: context.Context):
        if not ctx.voice_state.voice.is_paused():
            await ctx.send("It's not paused! Use `{ctx.prefix}pause` to pause.")

        elif ctx.voice_state.is_playing:
            ctx.voice_state.voice.resume()
            await ctx.message.add_reaction('⏯')

        else:
            await ctx.send("I'm not currently playing any music!")

    @commands.command(name='stop')
    async def _stop(self, ctx: context.Context):
        """Stop playing and clear the queue."""
        ctx.voice_state.songs.clear()

        if ctx.voice_state.is_playing:
            ctx.voice_state.skip()
            ctx.voice_state.voice.stop()
            await ctx.message.add_reaction('⏹')
        else:
            await ctx.send("I'm not currently playing any music!")

    @commands.command(name='skip', aliases=['next'])
    async def _skip(self, ctx: context.Context):
        """Skip the currently playing song.
        A number of skip votes are required depending on how many people there are in the voice channel.
        Server moderators can use the `forceskip` command to force this action."""
        if not ctx.voice_state.is_playing:
            return await ctx.send('Not playing any music right now...')

        voter = ctx.message.author
        vc = ctx.voice_state.voice.channel
        if ctx.author not in vc.members:
            return await ctx.send("You are not in the voice channel!")

        people_in_vc = len(vc.members) - 1
        if people_in_vc <= 2:
            required_votes = people_in_vc
        else:
            required_votes = math.floor(people_in_vc * 0.8)

        # if it reached the required vote amount
        if voter == ctx.voice_state.current.requester or len(ctx.voice_state.skip_votes) == required_votes:
            await ctx.message.add_reaction('⏭')
            ctx.voice_state.skip()

        elif voter.id not in ctx.voice_state.skip_votes:
            ctx.voice_state.skip_votes.append(voter.id)

            total_votes = len(ctx.voice_state.skip_votes)
            if total_votes >= required_votes:
                await ctx.message.add_reaction('⏭')
                ctx.voice_state.skip()
            else:
                await ctx.send('Skip vote added, currently at **{}/{}**'.format(total_votes, required_votes))

        else:
            await ctx.send('You have already voted to skip this song.')

    @commands.command(name='forceskip', aliases=['fskip'])
    @commands.has_permissions(manage_guild=True)
    async def _force_skip(self, ctx: context.Context):
        """Skip the currently playing song without requiring votes.

        You need the **Manage Server** permission to use this command."""
        if not ctx.voice_state.is_playing:
            return await ctx.send('Not playing any music right now...')

        await ctx.message.add_reaction('⏭')
        ctx.voice_state.skip()

    @commands.command(name='queue', aliases=['q'])
    async def _queue(self, ctx: context.Context):
        """See the current song queue."""
        if len(ctx.voice_state.songs) == 0:
            return await ctx.send(f'The queue is empty. Try queueing songs with `{ctx.prefix}play song_name`')

        blocks = []
        duration = 0
        # currently playing
        current = ctx.voice_state.current
        blocks.append(f"🎶 **[{current.source.title}]({current.source.url})**\n"
                      f"`{current.source.duration} | "
                      f"{current.requester}`\n")

        for i, song in enumerate(ctx.voice_state.songs, 1):
            blocks.append(f"**{i}**. **[{song.source.title}]({song.source.url})**\n"
                          f"`{song.source.duration} | "
                          f"{song.requester}`")
            duration += song.source.data.get('duration')

        embed = (discord.Embed(color=self.bot.main_color)
                 .set_author(icon_url=ctx.guild.icon_url, name=f"{ctx.guild.name} Music Queue - ")
                 .set_footer(text=f"{int(current.source.volume * 100)}% | "
                                  f"{len(ctx.voice_state.songs)} Songs | "
                                  f"{YTDLSource.parse_duration(duration)} in Total",
                             icon_url="https://i.imgur.com/T0532pn.png")
                 )
        await menu_stuff.paginate(self.bot, ctx, embed, blocks, item_per_page=5, add_page_info_to='author')

    @commands.command(name='shuffle')
    async def _shuffle(self, ctx: context.Context):
        """Shuffle the song queue."""
        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')

        ctx.voice_state.songs.shuffle()
        await ctx.message.add_reaction('✅')

    @commands.command(name='remove')
    async def _remove(self, ctx: context.Context, index: int):
        """Remove a song from the song queue."""
        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')

        if not 0 < index <= len(ctx.voice_state.songs):
            return await ctx.send("Please specify a proper index!")

        if ctx.author.id != ctx.voice_state.songs[index - 1].requester.id:
            return await ctx.send("You are not the requester of this song!")

        ctx.voice_state.songs.remove(index - 1)
        await ctx.message.add_reaction('✅')

    # This command has been disabled due to issues its causing.
    # @commands.command(name='loop')
    # async def _loop(self, ctx: context.Context):
    #     if not ctx.voice_state.is_playing:
    #         return await ctx.send('Nothing being played at the moment.')
    #
    #     # Inverse boolean value to loop and unloop.
    #     ctx.voice_state.loop = not ctx.voice_state.loop
    #     await ctx.message.add_reaction('✅')

    @commands.command(name='play')
    async def _play(self, ctx: context.Context, *, search: str):
        """Queue a song to play!"""
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise MusicError('You are not connected to any voice channel.')

        if ctx.voice_client:
            if ctx.voice_client.channel != ctx.author.voice.channel:
                raise MusicError('Bot is already in a voice channel.')

        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        msg = await ctx.send("I'm processing your request, please wait...")

        # checks
        async with ctx.typing():
            try:
                songs = await YTDLSource.create_source(ctx, search, loop=self.bot.loop)
            except (YTDLError, youtube_dl.DownloadError) as e:
                await msg.edit(content=str(e))
            else:
                for song in songs:
                    s_obj = Song(song)
                    await ctx.voice_state.songs.put(s_obj)

                # if its a playlist
                if len(songs) > 1:
                    await msg.edit(content=f'Added your playlist to the queue! '
                                           f'You can type `{ctx.prefix}queue` to see it.')
                else:
                    await msg.edit(content=f'Added **{s_obj.source.title}** to the queue! '
                                           f'You can type `{ctx.prefix}queue` to see it.')


def setup(bot):
    bot.add_cog(Music(bot))
