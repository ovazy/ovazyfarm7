import asyncio
import random
import re
import sys
import time
import traceback
import warnings
import zlib

from aiotfm.packet import Packet
from aiotfm.utils import Locale, get_keys, shakikoo
from aiotfm.connection import Connection
from aiotfm.player import Profile, Player
from aiotfm.tribe import Tribe
from aiotfm.friend import Friend
from aiotfm.message import Message, Whisper, Channel, ChannelMessage, Command
from aiotfm.shop import Shop
from aiotfm.inventory import Inventory, InventoryItem, Trade
from aiotfm.room import Map, Room, RoomList
from aiotfm.enums import TradeError, Community
from aiotfm.errors import AiotfmException, InvalidEvent, CommunityPlatformError, \
	AlreadyConnected, AlreadyRegistered, IncorrectPassword, LoginError, ServerUnreachable
	
def get_data(xml, tag, reversed=False):
	data = []

	tags = re.compile("\<" + tag + "(.*?)/\>")
	for elem in tags.finditer(xml):
		props = re.compile(r"(\-?\w+) ?= ?\"(\d+\.?\d*)\"")
		coords = {}
		for prop in props.finditer(elem.group(0)):
			val = int(float(prop.group(2)))
			prop_name = prop.group(1)
			if prop_name == "X":
				if reversed:
					val = 800 - val
			coords[prop_name] = val
		data.append(coords)
	return data
	
def get_map_length(xml):
	r = re.compile(r'\<C\>\<P(.*?)L="(\d+)"(.*?)/\>\<Z\>')
	length = r.match(xml)
	if length:
		return int(length.group(2))
	return 800

class Client:
	"""Represents a client that connects to Transformice.
	Two argument can be passed to the :class:`Client`.

	.. _event loop: https://docs.python.org/3/library/asyncio-eventloops.html

	Parameters
	----------
	community: Optional[:class:`int`]
		Defines the community of the client. Defaults to 0 (EN community).
	auto_restart: Optional[:class:`bool`]
		Whether the client should automatically restart on error. Defaults to False.
	loop: Optional[event loop]
		The `event loop`_ to use for asynchronous operations. If ``None`` is passed (defaults),
		the event loop used will be ``asyncio.get_event_loop()``.

	Attributes
	----------
	username: Optional[:class:`str`]
		The bot's username received from the server. Might be None if the bot didn't log in yet.
	room: Optional[:class:`aiotfm.room.Room`]
		The bot's room. Might be None if the bot didn't log in yet or couldn't join any room yet.
	trade: Optional[:class:`aiotfm.inventory.Trade`]
		The current trade that's going on (i.e: both traders accepted it).
	trades: :class:`list`[:class:`aiotfm.inventory.Trade`]
		All the trades that the bot participates. Most of them might be invitations only.
	inventory: Optional[:class:`aiotfm.inventory.Inventory`]
		The bot's inventory. Might be None if the bot didn't log in yet or it didn't receive
		anything.
	locale: :class:`aiotfm.locale.Locale`
		The bot's locale (translations).
	"""
	LOG_UNHANDLED_PACKETS = False

	def __init__(self, community=Community.en, auto_restart=False, bot_role=False, loop=None):
		self.loop = loop or asyncio.get_event_loop()
		
		self.command_prefix = '!'

		self.main = Connection('main', self, self.loop)
		self.bulle = None

		self._waiters = {}

		self._room = None
		self.trade = None
		self.trades = {}
		self.inventory = None

		self.username = None
		self.locale = Locale()
		self.community = Community(community)
		self.cp_fingerprint = 0

		self.keys = None
		self.authkey = 0

		self.auto_restart = auto_restart

		self._restarting = False
		self.bot_role = bot_role

		self._channels = []

	def data_received(self, data, connection):
		"""|coro|
		Dispatches the received data.

		:param data: :class:`bytes` the received data.
		:param connection: :class:`aiotfm.Connection` the connection that received
			the data.
		"""
		# :desc: Called when a socket receives a packet. Does not interfere
		# with :meth:`Client.handle_packet`.
		# :param connection: :class:`aiotfm.Connection` the connection that received
		# the packet.
		# :param packet: :class:`aiotfm.Packet` a copy of the packet.
		self.dispatch('raw_socket', connection, Packet(data))
		try:
			self.loop.create_task(self.handle_packet(connection, Packet(data)))
		except Exception:
			traceback.print_exc()

	async def handle_packet(self, connection: Connection, packet: Packet):
		"""|coro|
		Handles the known packets and dispatches events.
		Subclasses should handle only the unhandled packets from this method.

		Example: ::
			class Bot(aiotfm.Client):
				async def handle_packet(self, conn, packet):
					handled = await super().handle_packet(conn, packet.copy())

					if not handled:
						# Handle here the unhandled packets.
						pass

		:param connection: :class:`aiotfm.Connection` the connection that received
			the packet.
		:param packet: :class:`aiotfm.Packet` the packet.
		:return: True if the packet got handled, False otherwise.
		"""
		CCC = packet.readCode()
		if CCC == (1, 1): # Old packets
			oldCCC, *data = packet.readString().split(b'\x01')
			data = list(map(bytes.decode, data))
			oldCCC = tuple(oldCCC[:2])

			# :desc: Called when an old packet is received. Does not interfere
			# with :meth:`Client.handle_old_packet`.
			# :param connection: :class:`aiotfm.Connection` the connection that received
			# the packet.
			# :param oldCCC: :class:`tuple` the packet identifiers on the old protocol.
			# :param data: :class:`list` the packet data.
			self.dispatch('old_packet', connection, oldCCC, data)
			return await self.handle_old_packet(connection, oldCCC, data)
			
		elif CCC == (4, 4): # Player movement
			player = self._room.get_player(pid=packet.read32())
			if player is None:
				return

			packet.read32() # Round code

			player.moving_right, player.moving_left = packet.readBool(), packet.readBool()
			if player.moving_right and not player.moving_left:
				player.facing_right = True
			elif player.moving_left and not player.moving_right:
				player.facing_right = False

			player.x, player.y = packet.read32(), packet.read32()
			player.vx, player.vy = packet.read16(), packet.read16()

			player.jumping = packet.readBool()

			player.frame, player.on_portal = packet.read8(), packet.read8()
			
			self.dispatch('player_movement', player)
			
		elif CCC == (4, 6): # Player redirection
			player = self._room.get_player(pid=packet.read32())
			if player is None:
				return
				
			player.facing_right = packet.readBool()
			
			self.dispatch('player_direction_change', player)
		
		elif CCC == (4, 9): # Player duck
			player = self._room.get_player(pid=packet.read32())
			if player is None:
				return
				
			player.ducking = packet.readBool()
			
			self.dispatch('player_duck', player)
			
		elif CCC == (4, 10): # Player redirection
			player = self._room.get_player(pid=packet.read32())
			if player is None:
				return
				
			player.facing_right = packet.readBool()
			
			self.dispatch('player_direction_change', player)
			
		elif CCC == (5, 2):
			self._room.map.code = packet.read32()
			packet.read16()
			self._room.round_code = packet.read8()
			packet.read16()
			
			xml = ""
			try:
				xml = zlib.decompress(packet.readBytes(packet.read16())).decode('utf-8')
			except:
				pass
				
			packet.readUTF()
			self._room.map.perm = packet.read8()

			is_reversed = packet.readBool()
			self._room.map.is_reversed = is_reversed
			
			if xml:
				self._room.map.xml = xml
				self._room.map.length = get_map_length(xml)
				self._room.map.hole_pos = get_data(xml, 'T', reversed=is_reversed)
				self._room.map.cheese_pos = get_data(xml, 'F', reversed=is_reversed)
			else:
				self._room.map = Map()
				
			self.dispatch('map_change', self._room.map)

		if CCC == (5, 21): # Joined room
			self._room = Room(official=packet.readBool(), name=packet.readUTF())

			# :desc: Called when the client has joined a room.
			# :param room: :class:`aiotfm.room.Room` the room the client has entered.
			self.dispatch('joined_room', self._room)

		elif CCC == (5, 39): # Password required for the room

			# :desc: Called when a password is required to enter a room
			# :param room: :class:`aiotfm.room.Room` the room the server is asking for a password.
			self.dispatch('room_password', Room(packet.readUTF()))
			
		elif CCC == (6, 6): # Room message
			username = packet.readUTF()
			message = packet.readUTF()

			player = self._room.get_player(username=username)
			if player is None:
				player = Player(username)

			self.dispatch('room_message', Message(player, message, self))

			if message.startswith(self.command_prefix):
				command = Command(player, message, 'room', self)
				coro = getattr(self, command.command, None)
				if coro is not None:
					asyncio.ensure_future(self._run_command(coro, command.command, command, *command.args), loop=self.loop)
					
		elif CCC == (6, 9): # Room Lua message
			message = packet.readUTF()
			self.dispatch('room_lua_message', message)

		elif CCC == (6, 20): # Server message
			packet.readBool() # if False then the message will appear in the #Server channel
			t_key = packet.readUTF()
			t_args = [packet.readUTF() for i in range(packet.read8())]

			# :desc: Called when the client receives a message from the server that needs to be translated.
			# :param message: :class:`aiotfm.locale.Translation` the message translated with the
			# current locale.
			# :param *args: a list of string used as replacement inside the message.
			self.dispatch('server_message', self.locale[t_key], *t_args)

		elif CCC == (8, 1): # Play emote
			player = self._room.get_player(pid=packet.read32())
			emote = packet.read8()
			flag = packet.readUTF() if emote == 10 else ''

			# :desc: Called when a player plays an emote.
			# :param player: :class:`aiotfm.Player` the player.
			# :param emote: :class:`int` the emote's id.
			# :param flag: :class:`str` the flag's id.
			self.dispatch('emote', player, emote, flag)

		elif CCC == (8, 5): # Show emoji
			player = self._room.get_player(pid=packet.read32())
			emoji = packet.read8()

			# :desc: Called when a player is showing an emoji above its head.
			# :param player: :class:`aiotfm.Player` the player.
			# :param emoji: :class:`int` the emoji's id.
			self.dispatch('emoji', player, emoji)

		elif CCC == (8, 6): # Player won
			packet.read8()
			player = self._room.get_player(pid=packet.read32())
			player.score = packet.read16()
			order = packet.read8()
			player_time = packet.read16() / 100

			# :desc: Called when a player get the cheese to the hole.
			# :param player: :class:`aiotfm.Player` the player.
			# :param order: :class:`int` the order of the player in the hole.
			# :param player_time: :class:`float` player's time in the hole in seconds.
			self.dispatch('player_won', player, order, player_time)

		elif CCC == (8, 11): # Blue/pink Shaman
			for i, pid in enumerate([packet.read32(), packet.read32()]):
				player = self._room.get_player(pid=pid)
				if player is not None:
					player.isShaman = True
					if i == 1:
						player.isBlueShaman = True
					else:
						player.isPinkShaman = True
					self.dispatch('player_shaman_state_change', player)

		elif CCC == (8, 12): # Player Shaman state [True]
			player = self._room.get_player(pid=packet.read32())
			if player is not None:
				player.isShaman = False
				self.dispatch('player_shaman_state_change', player)

		elif CCC == (8, 16): # Profile
			# :desc: Called when the client receives the result of a /profile command.
			# :param profile: :class:`aiotfm.player.Profile` the profile.
			self.dispatch('profile', Profile(packet))
			
		elif CCC == (8, 19): # Player cheese state [True]
			player = self._room.get_player(pid=packet.read32())
			
			if player is not None:
				player.hasCheese = False	
				
				self.dispatch('player_cheese_state_change', player)

		elif CCC == (8, 20): # Shop
			# :desc: Called when the client receives the content of the shop.
			# :param shop: :class:`aiotfm.shop.Shop` the shop.
			self.dispatch('shop', Shop(packet))

		elif CCC == (8, 22): # Skills
			skills = {}
			for _ in range(packet.read8()):
				key, value = packet.read8(), packet.read8()
				skills[key] = value

			# :desc: Called when the client receives its skill tree.
			# :param skills: :class:`dict` the skills.
			self.dispatch('skills', skills)

		elif CCC == (16, 2): # Tribe invitation received
			author = packet.readUTF()
			tribe = packet.readUTF()

			# :desc: Called when the client receives an invitation to a tribe. (/inv)
			# :param author: :class:`str` the player that invited you.
			# :param tribe: :class:`str` the tribe.
			self.dispatch('tribe_inv', author, tribe)

		elif CCC == (26, 2): # Logged in successfully
			player_id = packet.read32()
			self.username = username = packet.readUTF()
			played_time = packet.read32()
			community = Community(packet.read8())
			pid = packet.read32()

			# :desc: Called when the client successfully logged in.
			# :param uid: :class:`int` the client's unique id.
			# :param username: :class:`str` the client's username.
			# :param played_time: :class:`int` the total number of minutes the client has played.
			# :param community: :class:`aiotfm.enums.Community` the community the client has connected to.
			# :param pid: :class:`int` the client's player id.
			self.dispatch('logged', player_id, username, played_time, community, pid)

		elif CCC == (26, 3): # Handshake OK
			online_players = packet.read32()
			language = packet.readUTF()
			country = packet.readUTF()
			self.authkey = packet.read32()

			self.loop.create_task(self._heartbeat_loop())

			await connection.send(Packet.new(176, 1).writeUTF(self.community.name))

			os_info = Packet.new(28, 17).writeString('en').writeString('Linux')
			os_info.writeString('LNX 29,0,0,140').write8(0)

			await connection.send(os_info)

			# :desc: Called when the client can login through the game.
			# :param online_players: :class:`int` the number of player connected to the game.
			# :param community: :class:`aiotfm.enums.Community` the community the server suggest.
			# :param country: :class:`str` the country detected from your ip.
			self.dispatch('login_ready', online_players, language, country)

		elif CCC == (26, 12): # Login result
			# :desc: Called when the client failed logging.
			# :param code: :class:`int` the error code.
			# :param code: :class:`str` error messages.
			# :param code: :class:`str` error messages.
			self.dispatch('login_result', packet.read8(), packet.readUTF(), packet.readUTF())

		elif CCC == (26, 25): # Ping
			# :desc: Called when the client receives the ping response from the server.
			self.dispatch('ping')

		elif CCC == (26, 35): # Room list
			roomlist = RoomList.from_packet(packet)
			# :desc: Dispatched when the client receives the room list
			self.dispatch('room_list', roomlist)

		elif CCC == (28, 5): # /mapcrew, /mod
			packet.read16()
			self.dispatch('staff_list', packet.readUTF())

		elif CCC == (28, 6): # Server ping
			await connection.send(Packet.new(28, 6).write8(packet.read8()))

		elif CCC == (28, 88): # Server reboot
			self.dispatch('server_reboot', packet.read32())

		elif CCC == (29, 6): # Lua logs
			# :desc: Called when the client receives lua logs from #Lua.
			# :param log: :class:`str` a log message.
			self.dispatch('lua_log', packet.readUTF())
			
		elif CCC == (29, 20): # Lua Text Area
			_id = packet.read32()
			content = packet.readUTF()
			
			callback_list = []
			if "<a href=" in content:
				a_tag = re.compile(r"\<a .*?event:(.*?)['\"]*\>")
				callback_list = [callback.group(1) for callback in a_tag.finditer(content)]
			self.dispatch('text_area', _id, content, callback_list)

		elif CCC == (31, 1): # Inventory data
			self.inventory = Inventory.from_packet(packet)
			self.inventory.client = self

			# :desc: Called when the client receives its inventory's content.
			# :param inventory: :class:`aiotfm.inventory.Inventory` the client's inventory.
			self.dispatch('inventory_update', self.inventory)

		elif CCC == (31, 2): # Update inventory item
			item_id = packet.read16()
			quantity = packet.read8()

			if item_id in self.inventory.items:
				item = self.inventory.items[item_id]
				previous = item.quantity
				item.quantity = quantity

				# :desc: Called when the quantity of an item has been updated.
				# :param item: :class:`aiotfm.inventory.InventoryItem` the new item.
				# :param previous: :class:`aiotfm.inventory.InventoryItem` the previous item.
				self.dispatch('item_update', item, previous)

			else:
				item = InventoryItem(item_id=item_id, quantity=quantity)
				self.inventory.items[item.id] = item

				# :desc: Called when the client receives a new item in its inventory.
				# :param item: :class:`aiotfm.inventory.InventoryItem` the new item.
				self.dispatch('new_item', item)

		elif CCC == (31, 5): # Trade invite
			pid = packet.read32()

			self.trades[pid] = Trade(self, self._room.get_player(pid=pid))

			# :desc: Called when received an invitation to trade.
			# :param trade: :class:`aiotfm.inventory.Trade` the trade object.
			self.dispatch('trade_invite', self.trades[pid])

		elif CCC == (31, 6): # Trade error
			name = packet.readUTF().lower()
			error = packet.read8()

			if name == self.username.lower():
				trade = self.trade
			else:
				for t in self.trades.values():
					if t.trader.lower() == name:
						trade = t
						break

			# :desc: Called when an error occurred with a trade.
			# :param trade: :class:`aiotfm.inventory.Trade` the trade that failed.
			# :param error: :class:`aiotfm.enums.TradeError` the error.
			self.dispatch('trade_error', trade, TradeError[error])
			trade._close()

		elif CCC == (31, 7): # Trade start
			pid = packet.read32()
			trade = self.trades.get(pid)

			if trade is None:
				raise AiotfmException(f'Cannot find the trade from pid {pid}.')

			trade._start()
			self.trade = trade

			# :desc: Called when a trade starts. You can access the trade object with `Client.trade`.
			self.dispatch('trade_start')

		elif CCC == (31, 8): # Trade items
			export = packet.readBool()
			id_ = packet.read16()
			quantity = (1 if packet.readBool() else -1) * packet.read8()

			items = self.trade.exports if export else self.trade.imports
			items.add(id_, quantity)

			trader = self if export else self.trade.trader
			self.trade.locked = [False, False]

			# :desc: Called when an item has been added/removed from the current trade.
			# :param trader: :class:`aiotfm.Player` the player that triggered the event.
			# :param id: :class:`int` the item's id.
			# :param quantity: :class:`int` the quantity added/removed. Can be negative.
			# :param item: :class:`aiotfm.inventory.InventoryItem` the item after the change.
			self.dispatch('trade_item_change', trader, id_, quantity, items.get(id_))

		elif CCC == (31, 9): # Trade lock
			index = packet.read8()
			locked = packet.readBool()
			if index > 1:
				self.trade.locked = [locked, locked]
				who = "both"
			else:
				self.trade.locked[index] = locked
				who = self.trade.trader if index == 0 else self

			# :desc: Called when the trade got (un)locked.
			# :param who: :class:`aiotfm.Player` the player that triggered the event.
			# :param locked: :class:`bool` either the trade got locked or unlocked.
			self.dispatch('trade_lock', who, locked)

		elif CCC == (31, 10): # Trade complete
			trade = self.trade
			self.trade._close(succeed=True)

		elif CCC == (44, 1): # Bulle switching
			timestamp = packet.read32()
			uid = packet.read32()
			pid = packet.read32()
			bulle_ip = packet.readUTF()
			ports = packet.readUTF().split('-')

			if self.bulle is not None:
				self.bulle.close()

			self.bulle = Connection('bulle', self, self.loop)
			await self.bulle.connect(bulle_ip, random.choice(ports))
			await self.bulle.send(Packet.new(44, 1).write32(timestamp).write32(uid).write32(pid))

		elif CCC == (44, 22): # Fingerprint offset changed
			connection.fingerprint = packet.read8()

		elif CCC == (60, 3): # Community platform
			TC = packet.read16()

			# :desc: Called when the client receives a packet from the community platform.
			# :param TC: :class:`int` the packet's code.
			# :param packet: :class:`aiotfm.Packet` the packet.
			self.dispatch('raw_cp', TC, packet.copy(copy_pos=True))

			if TC == 3: # Connected to the community platform
				# :desc: Called when the client is successfully connected to the community platform.
				self.dispatch('ready')

			elif TC == 35: # Player room change
				pid = packet.read32()
				username = packet.readUTF()

				packet.read8() # ???
				packet.read32() # ???
				packet.readBool() # ???
				packet.readBool() # ???
				packet.read32() # ???

				room_name = packet.readUTF()

				self.dispatch('player_room_change', username, room_name)

			elif TC == 55: # Channel join result
				sequenceId = packet.read32()
				result = packet.read8()

				# :desc: Called when the client receives the result of joining a channel.
				# :param sequenceId: :class:`int` identifier returned by :meth:`Client.sendCP`.
				# :param result: :class:`int` result code.
				self.dispatch('channel_joined_result', sequenceId, result)

			elif TC == 57: # Channel leave result
				sequenceId = packet.read32()
				result = packet.read8()

				# :desc: Called when the client receives the result of leaving a channel.
				# :param sequenceId: :class:`int` identifier returned by :meth:`Client.sendCP`.
				# :param result: :class:`int` result code.
				self.dispatch('channel_left_result', sequenceId, result)

			elif TC == 59: # Channel /who result
				idSequence = packet.read32()
				result = packet.read8()
				players = [Player(packet.readUTF()) for _ in range(packet.read16())]

				# :desc: Called when the client receives the result of the /who command in a channel.
				# :param idSequence: :class:`int` the reference to the packet that performed the request.
				# :param players: List[:class:`aiotfm.Player`] the list of players inside the channel.
				self.dispatch('channel_who', idSequence, players)

			elif TC == 62: # Joined a channel
				name = packet.readUTF()

				if name in self._channels:
					channel = [c for c in self._channels if c == name][0]
				else:
					channel = Channel(name, self)
					self._channels.append(channel)

				# :desc: Called when the client joined a channel.
				# :param channel: :class:`aiotfm.message.Channel` the channel.
				self.dispatch('channel_joined', channel)

			elif TC == 63: # Quit a channel
				name = packet.readUTF()
				if name in self._channels:
					self._channels.remove(name)

				# :desc: Called when the client leaves a channel.
				# :param name: :class:`str` the channel's name.
				self.dispatch('channel_closed', name)

			elif TC == 64: # Channel message
				username, community = packet.readUTF(), packet.read32()
				channel_name, message = packet.readUTF(), packet.readUTF()
				channel = self.get_channel(channel_name)

				author = self._room.get_player(username=username)
				if author is None:
					author = Player(username)

				if channel is None:
					channel = Channel(channel_name, self)
					self._channels.append(channel)

				channel_message = ChannelMessage(author, community, message, channel)

				# :desc: Called when the client receives a message from a channel.
				# :param message: :class:`aiotfm.message.ChannelMessage` the message.
				self.dispatch('channel_message', channel_message)

			elif TC == 65: # Tribe message
				author, message = Player(packet.readUTF()), packet.readUTF()

				# :desc: Called when the client receives a message from the tribe.
				# :param author: :class:`str` the message's author.
				# :param message: :class:`str` the message's content.
				self.dispatch('tribe_message', author, message)
				
				if message.startswith(self.command_prefix):
					command = Command(author, message, 'tribe', self)
					coro = getattr(self, command.command, None)
					if coro is not None:
						asyncio.ensure_future(self._run_command(coro, command.command, command, *command.args), loop=self.loop)

			elif TC == 66: # Whisper
				author = Player(packet.readUTF())
				commu = packet.read32()
				receiver = Player(packet.readUTF())
				message = packet.readUTF()

				author = self._room.get_player(name=author, default=author)
				receiver = self._room.get_player(name=receiver, default=receiver)

				# :desc: Called when the client receives a whisper.
				# :param message: :class:`aiotfm.message.Whisper` the message.
				self.dispatch('whisper', Whisper(author, commu, receiver, message, self))
				
				if message.startswith(self.command_prefix):
					command = Command(author, message, 'whisper', self)
					coro = getattr(self, command.command, None)
					if coro is not None:
						asyncio.ensure_future(self._run_command(coro, command.command, command, *command.args), loop=self.loop)

			elif TC == 86: # Tribe recruitment
				self.dispatch('tribe_recruitment', packet.readUTF(), packet.readUTF())

			elif TC == 88: # tribe member connected
				# :desc: Called when a tribe member connected.
				# :param name: :class:`str` the member's name.
				self.dispatch('member_connected', packet.readUTF())

			elif TC == 90: # tribe member disconnected
				# :desc: Called when a tribe member disconnected.
				# :param name: :class:`str` the member's name.
				self.dispatch('member_disconnected', packet.readUTF())

			else:
				if self.LOG_UNHANDLED_PACKETS:
					print(CCC, TC, bytes(packet.buffer)[4:])
				return False

		elif CCC == (100, 67): # New inventory item
			slot = packet.read8()
			slot = None if slot == 0 else slot
			item_id = packet.read16()
			quantity = packet.read8()

			item = InventoryItem(item_id=item_id, quantity=quantity, slot=slot)
			self.inventory[item_id] = item
			self.dispatch('new_item', item)

		elif CCC == (144, 1): # Set player list
			before = self._room.players
			self._room.players = {}

			for _ in range(packet.read16()):
				player = Player.from_packet(packet)
				self._room.players[player.pid] = player

			# :desc: Called when the client receives an update of all player in the room.
			# :param before: Dict[:class:`aiotfm.Player`] the list of player before the update.
			# :param players: Dict[:class:`aiotfm.Player`] the list of player updated.
			self.dispatch('bulk_player_update', before, self._room.players)

		elif CCC == (144, 2): # Add a player
			after = Player.from_packet(packet)
			before = self._room.players.pop(after.pid, None)

			self._room.players[after.pid] = after
			if before is None:
				# :desc: Called when a player joined the room.
				# :param player: :class:`aiotfm.Player` the player.
				self.dispatch('player_join', after)
			else:
				# :desc: Called when a player's data on the room has been updated.
				# :param before: :class:`aiotfm.Player` the player before the update.
				# :param player: :class:`aiotfm.Player` the player updated.
				self.dispatch('player_update', before, after)
				
		elif CCC == (144, 6): # Player cheese state [False]
			player = self._room.get_player(pid=packet.read32())
			
			if player is not None:
				player.hasCheese = packet.readBool()
				
				self.dispatch('player_cheese_state_change', player)

		elif CCC == (144, 7): # Player Shaman state [False]
			player = self._room.get_player(pid=packet.read32())
			if player is not None:
				player.isShaman = False
				self.dispatch('player_shaman_state_change', player)

		else:
			if self.LOG_UNHANDLED_PACKETS:
				print(CCC, bytes(packet.buffer)[2:])
			return False

		return True

	async def handle_old_packet(self, connection: Connection, oldCCC: tuple, data: list):
		"""|coro|
		Handles the known packets from the old protocol and dispatches events.
		Subclasses should handle only the unhandled packets from this method.

		Example: ::
			class Bot(aiotfm.Client):
				async def handle_old_packet(self, conn, oldCCC, data):
					handled = await super().handle_old_packet(conn, data.copy())

					if not handled:
						# Handle here the unhandled packets.
						pass

		:param connection: :class:`aiotfm.Connection` the connection that received
			the packet.
		:param oldCCC: :class:`tuple` the packet identifiers on the old protocol.
		:param data: :class:`list` the packet data.
		:return: True if the packet got handled, False otherwise.
		"""
		if oldCCC == (8, 5): # Player died
			player = self._room.get_player(pid=data[0])
			if player is not None:
				player.score = int(data[2])

				# :desc: Called when a player dies.
				# :param player: :class:`aiotfm.Player` the player.
				self.dispatch('player_died', player)

		elif oldCCC == (8, 7): # Remove a player
			player = self._room.players.pop(int(data[0]), None)

			if player is not None:
				# :desc: Called when a player leaves the room.
				# :param player: :class:`aiotfm.Player` the player.
				self.dispatch('player_remove', player)

		else:
			if self.LOG_UNHANDLED_PACKETS:
				print("[OLD]", oldCCC, data)
			return False

		return True

	async def _heartbeat_loop(self):
		"""|coro|
		Send a packet every fifteen seconds to stay connected to the game.
		"""
		last_heartbeat = 0
		while self.main.open:
			if self.loop.time() - last_heartbeat >= 15:
				t = time.perf_counter()
				await self.main.send(Packet.new(26, 26))
				if self.bulle is not None and self.bulle.open:
					await self.bulle.send(Packet.new(26, 26))

				# :desc: Called at each heartbeat.
				# :param time: :class:`float` the time took to send the keep-alive packet.
				self.dispatch('heartbeat', (time.perf_counter() - t) * 1000)
				last_heartbeat = self.loop.time()
			await asyncio.sleep(.5)

	def get_channel(self, name):
		"""Returns a channel from it's name or None if not found.
		:param name: :class:`str` the name of the channel.
		:return: :class:`aiotfm.message.ChannelMessage` or None
		"""
		if name is None:
			return None

		for channel in self._channels:
			if channel.name == name:
				return channel

	def get_trade(self, player):
		"""Returns the pending/current trade with a player.
		:param player: :class:`aiotfm.Player` or :class:`str` the player.
		:return: :class:`aiotfm.inventory.Trade` the trade with the player.
		"""
		if not isinstance(player, (str, Player)):
			raise TypeError(f"Expected Player or str types got {type(player)}")

		if isinstance(player, Player):
			return self.trades.get(player.pid)

		player = player.lower()
		for trade in self.trades.values():
			if trade.trader.lower() == player:
				return trade
				
	def command(self, coro):
		name = coro.__name__
		
		if not asyncio.iscoroutinefunction(coro):
			message = "Couldn't register a non-coroutine function for the command {}.".format(name)
			raise InvalidEvent(message)
		
		setattr(self, name, coro)
		return coro

	def event(self, coro):
		"""A decorator that registers an event.

		More about events [here](Events.md).
		"""
		name = coro.__name__
		if not name.startswith('on_'):
			raise InvalidEvent("'{}' isn't a correct event naming.".format(name))
		if not asyncio.iscoroutinefunction(coro):
			message = "Couldn't register a non-coroutine function for the event {}.".format(name)
			raise InvalidEvent(message)

		setattr(self, name, coro)
		return coro

	def wait_for(self, event, condition=None, timeout=None, stopPropagation=False):
		"""Wait for an event.

		Example: ::
			@client.event
			async def on_room_message(author, message):
				if message == 'id':
					await client.sendCommand('profile '+author)
					profile = await client.wait_for('on_profile', lambda p: p.username == author)
					await client.sendRoomMessage('Your id: {}'.format(profile.id))

		:param event: :class:`str` the event name.
		:param condition: Optionnal[`function`] A predicate to check what to wait for.
			The arguments must meet the parameters of the event being waited for.
		:param timeout: Optionnal[:class:`int`] the number of seconds before
			throwing asyncio.TimeoutError
		:return: [`asyncio.Future`](https://docs.python.org/3/library/asyncio-future.html#asyncio.Future)
			a future that you must await.
		"""
		event = event.lower()
		future = self.loop.create_future()

		if condition is None:
			def everything(*a):
				return True
			condition = everything

		if event not in self._waiters:
			self._waiters[event] = []

		self._waiters[event].append((condition, future, stopPropagation))

		return asyncio.wait_for(future, timeout)

	async def _run_event(self, coro, event_name, *args, **kwargs):
		"""|coro|
		Runs an event and handle the error if any.

		:param coro: a coroutine function.
		:param event_name: :class:`str` the event's name.
		:param args: arguments to pass to the coro.
		:param kwargs: keyword arguments to pass to the coro.

		:return: :class:`bool` whether the event ran successfully or not
		"""
		try:
			await coro(*args, **kwargs)
			return True
		# except asyncio.CancelledError:
		# 	raise
		except Exception as e:
			if hasattr(self, 'on_error'):
				try:
					await self.on_error(event_name, e, *args, **kwargs)
				# except asyncio.CancelledError:
				# 	raise
				except Exception:
					if not self.auto_restart:
						self.close()
				finally:
					if self.auto_restart:
						asyncio.ensure_future(self.restart_soon(), loop=self.loop)
		return False
		
	async def _run_command(self, coro, command_name, command, *args):
		try:
			await coro(command, *args)
			return True
		except Exception as e:
			if hasattr(self, 'on_error'):
				try:
					await self.on_error(command_name, e, *args)
				except Exception as e:
					if not self.auto_restart:
						self.close()
				finally:
					if self.auto_restart:
						asyncio.ensure_future(self.restart_soon(), loop=self.loop)

	def dispatch(self, event, *args, **kwargs):
		"""Dispatches events

		:param event: :class:`str` event's name. (without 'on_')
		:param args: arguments to pass to the coro.
		:param kwargs: keyword arguments to pass to the coro.

		:return: [`Task`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task)
			the _run_event wrapper task
		"""
		method = 'on_' + event

		if method in self._waiters:
			to_remove = []
			waiters = self._waiters[method]
			for i, (cond, fut, stop) in enumerate(waiters):
				if fut.cancelled():
					to_remove.append(i)
					continue

				try:
					result = bool(cond(*args))
				except Exception as e:
					fut.set_exception(e)
				else:
					if result:
						fut.set_result(args[0] if len(args) == 1 else args if len(args) > 0 else None)
						if stop:
							del waiters[i]
							return None
						to_remove.append(i)

			if len(to_remove) == len(waiters):
				del self._waiters[method]
			else:
				for i in to_remove[::-1]:
					del waiters[i]

		coro = getattr(self, method, None)
		if coro is not None:
			dispatch = self._run_event(coro, method, *args, **kwargs)
			return asyncio.ensure_future(dispatch, loop=self.loop)

	async def on_error(self, event, err, *a, **kw):
		"""Default on_error event handler. Prints the traceback of the error."""
		message = '\nAn error occurred while dispatching the event "{0}":\n\n{2}'
		tb = traceback.format_exc(limit=-3)
		print(message.format(event, err, tb), file=sys.stderr)
		return message.format(event, err, tb)

	async def on_connection_error(self, conn, error):
		"""Default on_connection_error event handler. Prints the error."""
		print('{0.__class__.__name__}: {0}'.format(error), file=sys.stderr)

	async def on_login_result(self, code, *args):
		"""Default on_login_result handler. Raise an error and closes the connection."""
		self.loop.call_later(5, self.close)
		if code == 1:
			raise AlreadyConnected()
		if code == 2:
			raise IncorrectPassword()
		if code == 3:
			raise AlreadyRegistered()
		raise LoginError(code)

	async def connect(self):
		"""|coro|
		Creates a connection with the main server.
		"""

		for port in random.sample([13801, 11801, 12801, 14801], 4):
			try:
				await self.main.connect('94.23.249.129', port)
			except Exception:
				pass
			else:
				break
		else:
			raise ServerUnreachable('Unable to connect to the server.')

		while not self.main.open:
			await asyncio.sleep(.1)

	async def sendHandshake(self):
		"""|coro|
		Sends the handshake packet so the server recognizes this socket as a player.
		"""
		packet = Packet.new(28, 1).write16(self.keys.version).write8(8).writeString('en').writeString(self.keys.connection)
		packet.writeString('Desktop').writeString('-').write32(0x1fbd).writeString('')
		packet.writeString('74696720697320676f6e6e61206b696c6c206d7920626f742e20736f20736164')
		packet.writeString(
			"A=t&SA=t&SV=t&EV=t&MP3=t&AE=t&VE=t&ACC=t&PR=t&SP=f&SB=f&DEB=f&V=LNX 29,0,0,140&M=Adobe"
			" Linux&R=1920x1080&COL=color&AR=1.0&OS=Linux&ARCH=x86&L=en&IME=t&PR32=t&PR64=t&LS=en-U"
			"S&PT=Desktop&AVD=f&LFD=f&WD=f&TLS=t&ML=5.1&DP=72")
		packet.write32(0).write32(0x6257).writeString('')

		await self.main.send(packet)

	async def _start(self, keys=None):
		"""|coro|
		Starts the client.

		:param api_tfmid: :class:`int` or :class:`str` your Transformice id.
		:param api_token: :class:`str` your token to access the API.
		"""
		if keys is not None:
			self.keys = keys
		else:
			self.keys = await get_keys(self.loop)

		await self.connect()
		await self.sendHandshake()
		await self.locale.load()

	async def restart_soon(self, *args, delay=5.0, **kwargs):
		"""Restarts the client in several seconds.

		:param delay: :class:`int` the delay before restarting. Default is 5 seconds.
		:param args: arguments to pass to the :meth:`Client.restart` method.
		:param kwargs: keyword arguments to pass to the :meth:`Client.restart` method."""
		if self._restarting:
			return

		self._restarting = True
		await asyncio.sleep(delay)
		await self.restart(*args, **kwargs)
		self._restarting = False

	async def restart(self, keys=None):
		"""Restarts the client.

		:param keys:"""

		# :desc: Notify when the client restarts.
		self.dispatch("restart")

		self.close()

		# If we don't recreate the connection, we won't be able to connect.
		self.main = Connection('main', self, self.loop)
		self.bulle = None

		if keys is not None:
			self.keys = keys
		else:
			self.keys = keys = await get_keys(self.loop)

		await self.connect()
		await self.sendHandshake()
		await self.locale.load()

	async def login(self, username, password, encrypted=True, room='1'):
		"""|coro|
		Log in the game.

		:param username: :class:`str` the client username.
		:param password: :class:`str` the client password.
		:param encrypted: Optional[:class:`bool`] whether the password is already encrypted or not.
		:param room: Optional[:class:`str`] the room where the client will be logged in.
		"""
		if not encrypted:
			password = shakikoo(password)

		packet = Packet.new(26, 8).writeString(username).writeString(password)
		packet.writeString("app:/TransformiceAIR.swf/[[DYNAMIC]]/2/[[DYNAMIC]]/4")
		packet.writeString(room)
		if not self.bot_role:
			packet.write32(self.authkey ^ self.keys.auth)
		packet.write8(0).writeString('')
		if not self.bot_role:
			packet.cipher(self.keys.identification)
		packet.write8(0)

		await self.main.send(packet)

	async def register(self, username, password, email="abcdefgh@ijklmn.opq", captcha=""):
		packet = Packet.new(26, 7).writeString(username).writeString(password)
		packet.writeString(email)
		packet.writeString(captcha)
		packet.write16(0)
		packet.writeString("app:/TransformiceAIR.swf/[[DYNAMIC]]/2/[[DYNAMIC]]/4")
		packet.cipher(self.keys.identification)

		await self.main.send(packet)

	def run(self, username, password, **kwargs):
		"""A blocking call that do the event loop initialization for you.

		Equivalent to: ::
			@bot.event
			async def on_login_ready(*a):
				await bot.login(username, password)

			loop = asyncio.get_event_loop()
			loop.create_task(bot._start(api_id, api_token))
			loop.run_forever()
		"""
		if self.auto_restart:
			self.auto_restart = False
			warnings.warn(
				"The usage of Client.run and Client.auto_restart is not allowed. "
				"Use Client._start method to enable automatic restart."
			)

		keys = kwargs.pop('keys', None)
		asyncio.ensure_future(self._start(keys=keys), loop=self.loop)
		self.loop.run_until_complete(self.wait_for('on_login_ready'))
		asyncio.ensure_future(self.login(username, password, **kwargs), loop=self.loop)

		try:
			self.loop.run_forever()
		finally:
			self.loop.run_until_complete(self.loop.shutdown_asyncgens())
			self.loop.close()

	def close(self):
		"""Closes the sockets."""
		self.main.close()
		if self.bulle is not None:
			self.bulle.close()

		if not self.auto_restart and self.loop.is_running():
			self.loop.stop()

	async def sendCP(self, code, data=b''):
		"""|coro|
		Send a packet to the community platform.

		:param code: :class:`int` the community platform code.
		:param data: :class:`aiotfm.Packet` or :class:`bytes` the data.
		"""
		self.cp_fingerprint = fp = (self.cp_fingerprint + 1) % 0XFFFFFFFF

		packet = Packet.new(60, 3).write16(code)
		packet.write32(self.cp_fingerprint).writeBytes(data)
		await self.main.send(packet, cipher=True)

		return fp

	async def sendRoomMessage(self, message):
		"""|coro|
		Send a message to the room.

		:param message: :class:`str` the content of the message.
		"""
		packet = Packet.new(6, 6).writeString(message)

		await self.bulle.send(packet, cipher=True)

	async def sendTribeMessage(self, message):
		"""|coro|
		Send a message to the tribe.

		:param message: :class:`str` the content of the message.
		"""
		await self.sendCP(50, Packet().writeString(message))

	async def sendChannelMessage(self, channel, message):
		"""|coro|
		Send a message to a public channel.

		:param channel: :class:`str` the channel's name.
		:param message: :class:`str` the content of the message.
		"""
		if isinstance(channel, Channel):
			channel = channel.name

		return await self.sendCP(48, Packet().writeString(channel).writeString(message))

	async def whisper(self, username, message, overflow=False):
		"""|coro|
		Whisper to a player.

		:param username: :class:`str` the player to whisper.
		:param message: :class:`str` the content of the whisper.
		:param overflow: :class:`bool` will send the complete message if True, splitted
			in several messages.
		"""
		if isinstance(username, Player):
			username = username.username

		async def send(msg):
			await self.sendCP(52, Packet().writeString(username).writeString(msg))

		if isinstance(message, str):
			message = message.encode()
		message = message.replace(b'<', b'&lt;').replace(b'>', b'&gt;')

		await send(message[:255])
		for i in range(255, len(message), 255):
			await asyncio.sleep(1)
			await self.whisper(username, message[i:i + 255])

	async def silenceWhisper(self, state=3, message=""):
		await self.sendCP(60, Packet().write8(state).writeString(message))

	async def getTribe(self, disconnected=True):
		"""|coro|
		Gets the client's :class:`aiotfm.Tribe` and return it

		:param disconnected: :class:`bool` if True retrieves also the disconnected members.
		:return: :class:`aiotfm.Tribe` or ``None``.
		"""
		sid = self.cp_fingerprint + 1
		await self.sendCP(108, Packet().writeBool(disconnected))

		def is_tribe(tc, packet):
			return (tc == 109 and packet.read32() == sid) or tc == 130

		tc, packet = await self.wait_for('on_raw_cp', is_tribe, timeout=5)
		if tc == 109:
			result = packet.read8()
			if result == 1:
				tc, packet = await self.wait_for('on_raw_cp', lambda tc, p: tc == 130, timeout=5)
			elif result == 17:
				return None
			else:
				raise CommunityPlatformError(118, result)
		return Tribe(packet)

	async def getRoomList(self, gamemode=0, timeout=3):
		"""|coro|
		Get the room list

		:param gamemode: Optional[:class:`aiotfm.enums.GameMode`] the room's gamemode.
		:param timeout: Optional[:class:`int`] timeout in seconds. Defaults to 3 seconds.
		:return: :class:`aiotfm.room.RoomList` the room list for the given gamemode or None
		"""
		await self.main.send(Packet.new(26, 35).write8(int(gamemode)))

		def predicate(roomlist):
			return gamemode == 0 or roomlist.gamemode == gamemode

		try:
			return await self.wait_for('on_room_list', predicate, timeout=timeout)
		except asyncio.TimeoutError:
			return None
		
	async def addFriend(self, player):
		await self.sendCP(18, Packet().writeString(player))
		
	async def getFriendList(self):
		"""|coro|
		Get the client's friend list

		:return: List[:class:`aiotfm.Friend`]  List of friends
		"""
		await self.sendCP(28)

		def is_friend_list(tc, packet):
			return tc == 34

		tc, packet = await self.wait_for('on_raw_cp', is_friend_list, timeout=5)
		return Friend.from_packet(packet)

	async def playEmote(self, emote, flag='be'):
		"""|coro|
		Play an emote.

		:param emote: :class:`int` the emote's id.
		:param flag: Optional[:class:`str`] the flag for the emote id 10. Defaults to 'be'.
		"""
		packet = Packet.new(8, 1).write8(emote).write32(0)
		if emote == 10:
			packet.writeString(flag)

		await self.bulle.send(packet)

	async def sendSmiley(self, smiley):
		"""|coro|
		Makes the client showing a smiley above it's head.

		:param smiley: :class:`int` the smiley's id. (from 0 to 9)
		"""
		if smiley < 0 or smiley > 9:
			raise AiotfmException('Invalid smiley id')

		packet = Packet.new(8, 5).write8(smiley)

		await self.bulle.send(packet)

	async def loadLua(self, lua_code):
		"""|coro|
		Load a lua code in the room.

		:param lua_code: :class:`str` or :class:`bytes` the lua code to send.
		"""
		if isinstance(lua_code, str):
			lua_code = lua_code.encode()

		packet = Packet.new(29, 1).write24(len(lua_code)).writeBytes(lua_code)

		await self.bulle.send(packet)

	async def sendCommand(self, command):
		"""|coro|
		Send a command to the game.

		:param command: :class:`str` the command to send.
		"""
		packet = Packet.new(6, 26).writeString(command[:255])

		await self.main.send(packet, cipher=True)

	async def enterTribe(self):
		"""|coro|
		Enter the tribe house
		"""
		await self.main.send(Packet.new(16, 1))

	async def enterTribeHouse(self):
		"""|coro|
		Alias for :meth:`Client.enterTribe`
		"""
		await self.enterTribe()

	async def joinRoom(self, room_name, password=None, community=None, auto=False):
		"""|coro|
		Join a room.
		The event 'on_joined_room' is dispatched when the client has successfully joined the room.

		:param password: :class:`str` if given the client will ignore `community` and `auto` parameters
			and will connect to the room with the given password.
		:param room_name: :class:`str` the room's name.
		:param community: Optional[:class:`int`] the room's community.
		:param auto: Optional[:class:`bool`] joins a random room (I think).
		"""
		if password is not None:
			packet = Packet.new(5, 39).writeString(password).writeString(room_name)
		else:
			packet = Packet.new(5, 38).writeString(Community(community or self.community).name)
			packet.writeString(room_name).writeBool(auto)

		await self.main.send(packet)

	async def joinChannel(self, name, permanent=True):
		"""|coro|
		Join a #channel.
		The event 'on_channel_joined' is dispatched when the client has successfully joined
		a channel.

		:param name: :class:`str` the channel's name
		:param permanent: Optional[:class:`bool`] if True (default) the server will automatically
		reconnect the user to this channel when logged in.
		"""
		await self.sendCP(54, Packet().writeString(name).writeBool(permanent))

	async def leaveChannel(self, channel):
		"""|coro|
		Leaves a #channel.

		:param channel: :class:`aiotfm.message.Channel` channel to leave.
		"""
		if isinstance(channel, Channel):
			name = channel.name
		else:
			name = channel

		await self.sendCP(56, Packet().writeString(name))

	async def enterInvTribeHouse(self, author):
		"""|coro|
		Join the tribe house of another player after receiving an /inv.

		:param author: :class:`str` the author's username who sent the invitation.
		"""
		await self.main.send(Packet.new(16, 2).writeString(author))

	async def recruit(self, player):
		"""|coro|
		Send a recruit request to a player.

		:param player: :class:`str` the player's username you want to recruit.
		"""
		await self.sendCP(78, Packet().writeString(player))

	async def requestShopList(self):
		"""|coro|
		Send a request to the server to get the shop list."""
		await self.main.send(Packet.new(8, 20))

	async def startTrade(self, player):
		"""|coro|
		Starts a trade with the given player.

		:param player: :class:`aiotfm.Player` the player to trade with.
		:return: :class:`aiotfm.inventory.Trade` the resulting trade"""
		if isinstance(player, Player) and player.pid == -1:
			player = player.username

		if isinstance(player, str):
			player = self._room.get_player(username=player)
			if player is None:
				raise AiotfmException("The player must be in your room to start a trade.")

		trade = Trade(self, player)

		self.trades[player.pid] = trade
		await trade.accept()
		return trade

	async def requestInventory(self):
		"""|coro|
		Send a request to the server to get the bot's inventory."""
		await self.main.send(Packet.new(31, 1))
		
	async def getProfile(self, username, timeout=3):
		username = username.lower()
		def check(p):
			if '#' not in username:
				return p.username.split('#')[0].lower()==username
			return p.username.lower()==username

		try:
			await self.sendCommand('profile {}'.format(username))
			return await self.wait_for('on_profile', check, timeout=timeout)
		except asyncio.TimeoutError:
			return None
