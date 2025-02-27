# -*- coding: utf-8 -*-

import asyncio
import random
import unittest
from datetime import datetime, timezone

from aiohttp.test_utils import AioHTTPTestCase
from mongomock_motor import AsyncMongoMockClient

from arena_new import ArenaTournament
from const import (
    BYEGAME,
    STARTED,
    ARENA,
    RR,
    SWISS,
    T_CREATED,
    T_STARTED,
    T_FINISHED,
    TEST_PREFIX,
)
from draw import draw
from fairy import BLACK
from game import MAX_PLY
from glicko2.glicko2 import DEFAULT_PERF
from newid import id8
from pychess_global_app_state_utils import get_app_state
from rr import RRTournament
from server import make_app
from swiss import SwissTournament
from tournament import Tournament
from tournaments import upsert_tournament_to_db, new_tournament
from user import User
from utils import play_move
from logger import handler
from variants import VARIANTS

import logging

log = logging.getLogger(__name__)
logging.getLogger().removeHandler(handler)

# from misc import timeit

PERFS = {variant: DEFAULT_PERF for variant in VARIANTS}

ONE_TEST_ONLY = False


class TestTournament(Tournament):
    async def join_players(self, nb_players):
        self.game_tasks = set()

        for i in range(1, nb_players + 1):
            name = "%sUser_%s" % (TEST_PREFIX, i)
            player = User(self.app_state, username=name, title="TEST", perfs=PERFS)
            self.app_state.users[player.username] = player
            player.tournament_sockets[self.id] = set((None,))
            await self.join(player)

    async def create_new_pairings(self, waiting_players):
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print("--- create_new_pairings at %s ---" % now)
        self.print_leaderboard()
        pairing, games = await Tournament.create_new_pairings(self, waiting_players)

        # aouto play test games
        # for wp, bp in pairing:
        #     print("%s - %s" % (wp.username, bp.username))
        print("--- create_new_pairings done ---")

        for game in games:
            if game.status == BYEGAME:  # ByeGame
                continue
            self.app_state.games[game.id] = game
            game.random_mover = True
            self.game_tasks.add(asyncio.create_task(self.play_random(game)))

    # @timeit
    async def play_random(self, game):
        """Play random moves for TEST players"""
        if game.status == BYEGAME:  # ByeGame
            return

        if self.system == ARENA:
            if random.choice((True, False)):
                game.berserk("white")

            if random.choice((True, False)):
                game.berserk("black")

            await asyncio.sleep(random.choice((0, 0.1, 0.3, 0.5, 0.7)))

        game.status = STARTED
        while game.status <= STARTED:
            cur_player = game.bplayer if game.board.color == BLACK else game.wplayer
            opp_player = game.wplayer if game.board.color == BLACK else game.bplayer
            if cur_player.title == "TEST":
                ply = random.randint(20, int(MAX_PLY / 10))
                if game.board.ply == ply or game.board.ply > 60:
                    player = game.wplayer if ply % 2 == 0 else game.bplayer
                    if game.board.ply > 60:
                        response = await draw(game, cur_player.username, agreement=True)
                    else:
                        response = await game.game_ended(player, "resign")
                    if opp_player.title != "TEST":
                        await opp_player.send_game_message(game.id, response)
                else:
                    move = random.choice(game.legal_moves)
                    clocks = (game.clocks_w[-1], game.clocks_b[-1])
                    await play_move(self.app_state, cur_player, game, move, clocks=clocks)
            await asyncio.sleep(0.01)


class ArenaTestTournament(TestTournament, ArenaTournament):
    system = ARENA

    def create_pairing(self, waiting_players):
        return ArenaTournament.create_pairing(self, waiting_players)


class RRTestTournament(TestTournament, RRTournament):
    system = RR

    def create_pairing(self, waiting_players):
        return RRTournament.create_pairing(self, waiting_players)


class SwissTestTournament(TestTournament, SwissTournament):
    system = SWISS

    def create_pairing(self, waiting_players):
        return SwissTournament.create_pairing(self, waiting_players)


async def create_dev_arena_tournament(app):
    data = {
        "name": "3. zh960 test arena",
        "createdBy": "gbtami",
        "variant": "crazyhouse",
        "chess960": True,
        "base": 1,
        "inc": 1,
        "system": ARENA,
        "beforeStart": 15,
        "minutes": 25,
    }
    await new_tournament(get_app_state(app), data)


async def create_arena_test(app):
    app_state = get_app_state(app)
    tid = "12345678"
    await app_state.db.tournament.delete_one({"_id": tid})
    await app_state.db.tournament_player.delete_many({"tid": tid})
    await app_state.db.tournament_pairing.delete_many({"tid": tid})

    tournament = ArenaTestTournament(
        app_state,
        tid,
        variant="gorogoroplus",
        name="Test Arena",
        chess960=False,
        base=1,
        before_start=0.1,
        minutes=3,
        created_by="PyChess",
    )
    #    tournament = SwissTestTournament(app, tid, variant="makpong", name="First Makpong Swiss", before_start=0.1, rounds=7, created_by="PyChess")
    #    tournament = RRTestTournament(app, tid, variant="makpong", name="First Makpong RR", before_start=0.1, rounds=7, created_by="PyChess")
    app_state.tournaments[tid] = tournament
    app_state.tourneysockets[tid] = {}

    await upsert_tournament_to_db(tournament, app_state)

    #    await tournament.join_players(6)
    await tournament.join_players(19)


class TournamentTestCase(AioHTTPTestCase):
    async def tearDownAsync(self):
        app_state = get_app_state(self.app)
        has_games = len(app_state.games) > 0

        for game in app_state.games.values():
            if game.status == BYEGAME:  # ByeGame
                continue
            if game.status <= STARTED:
                await game.abort_by_server()

            if game.remove_task is not None:
                game.remove_task.cancel()
                try:
                    await game.remove_task
                except asyncio.CancelledError:
                    pass

        if has_games:
            for task in self.tournament.game_tasks:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        await self.client.close()

    async def get_application(self):
        app = make_app(db_client=AsyncMongoMockClient())
        return app

    @unittest.skipIf(ONE_TEST_ONLY, "1 test only")
    async def test_tournament_without_players(self):
        app_state = get_app_state(self.app)
        # app_state.db = None
        tid = id8()
        self.tournament = ArenaTestTournament(app_state, tid, before_start=0, minutes=2.0 / 60.0)
        app_state.tournaments[tid] = self.tournament

        self.assertEqual(self.tournament.status, T_CREATED)

        await asyncio.sleep(0.1)
        self.assertEqual(self.tournament.status, T_STARTED)

        await asyncio.sleep(3)
        self.assertEqual(self.tournament.status, T_FINISHED)

        await self.tournament.clock_task

    @unittest.skipIf(ONE_TEST_ONLY, "1 test only")
    async def test_tournament_players(self):
        app_state = get_app_state(self.app)
        # app_state.db = None
        NB_PLAYERS = 15
        tid = id8()
        self.tournament = ArenaTestTournament(app_state, tid, before_start=0, minutes=0)
        app_state.tournaments[tid] = self.tournament
        await self.tournament.join_players(NB_PLAYERS)

        self.assertEqual(len(self.tournament.leaderboard), NB_PLAYERS)

        withdrawn_player = next(iter(self.tournament.players))
        await self.tournament.withdraw(withdrawn_player)

        self.assertNotIn(withdrawn_player, self.tournament.leaderboard)
        self.assertEqual(len(self.tournament.players), NB_PLAYERS)
        self.assertEqual(len(self.tournament.leaderboard), NB_PLAYERS - 1)

        await self.tournament.clock_task

        self.assertEqual(self.tournament.status, T_FINISHED)

    @unittest.skipIf(ONE_TEST_ONLY, "1 test only")
    async def test_tournament_with_3_active_players(self):
        app_state = get_app_state(self.app)
        # app_state.db = None
        NB_PLAYERS = 15
        tid = id8()
        self.tournament = ArenaTestTournament(app_state, tid, before_start=0.1, minutes=1)
        app_state.tournaments[tid] = self.tournament
        await self.tournament.join_players(NB_PLAYERS)

        # 12 player leave the tournament lobby
        for i in range(12):
            print(i)
            del list(self.tournament.players.keys())[i].tournament_sockets[self.tournament.id]
        self.assertEqual(len(self.tournament.waiting_players()), NB_PLAYERS - 12)

        await self.tournament.clock_task

        self.assertEqual(self.tournament.status, T_FINISHED)

        for user in self.tournament.players:
            self.assertTrue(self.tournament.players[user].nb_not_paired <= 1)

    @unittest.skipIf(ONE_TEST_ONLY, "1 test only")
    async def test_tournament_pairing_5_round_SWISS(self):
        app_state = get_app_state(self.app)
        # app_state.db = None
        NB_PLAYERS = 15
        NB_ROUNDS = 5
        tid = id8()
        self.tournament = SwissTestTournament(app_state, tid, before_start=0, rounds=NB_ROUNDS)
        app_state.tournaments[tid] = self.tournament
        await self.tournament.join_players(NB_PLAYERS)

        await self.tournament.clock_task

        self.assertEqual(self.tournament.status, T_FINISHED)
        self.assertEqual(
            [len(player.games) for player in self.tournament.players.values()],
            NB_PLAYERS * [NB_ROUNDS],
        )

    @unittest.skipIf(ONE_TEST_ONLY, "1 test only")
    async def test_tournament_pairing_1_min_ARENA(self):
        app_state = get_app_state(self.app)
        # app_state.db = None
        NB_PLAYERS = 15
        tid = id8()
        self.tournament = ArenaTestTournament(app_state, tid, before_start=0.1, minutes=1)
        app_state.tournaments[tid] = self.tournament
        await self.tournament.join_players(NB_PLAYERS)

        # withdraw one player
        await self.tournament.withdraw(list(self.tournament.players.keys())[-1])
        self.assertEqual(self.tournament.nb_players, NB_PLAYERS - 1)

        # make the first player leave the tournament lobby
        del list(self.tournament.players.keys())[0].tournament_sockets[self.tournament.id]

        self.assertEqual(len(self.tournament.waiting_players()), NB_PLAYERS - 2)

        await self.tournament.clock_task

        self.assertEqual(self.tournament.status, T_FINISHED)

    @unittest.skipIf(ONE_TEST_ONLY, "1 test only")
    async def test_tournament_pairing_5_round_RR(self):
        app_state = get_app_state(self.app)
        # app_state.db = None
        NB_PLAYERS = 5
        NB_ROUNDS = 5

        tid = id8()
        self.tournament = RRTestTournament(app_state, tid, before_start=0, rounds=NB_ROUNDS)
        app_state.tournaments[tid] = self.tournament
        await self.tournament.join_players(NB_PLAYERS)

        await self.tournament.clock_task

        self.assertEqual(self.tournament.status, T_FINISHED)
        self.assertEqual(
            [len(player.games) for player in self.tournament.players.values()],
            NB_PLAYERS * [NB_ROUNDS],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
