"""Tests for state machine — valid/invalid transitions, idempotency."""

from __future__ import annotations

from bot.models.types import TERMINAL_STATES, VALID_TRANSITIONS, DealState


class TestDealStateTransitions:
    def test_all_states_have_transitions(self):
        for state in DealState:
            if state in TERMINAL_STATES:
                continue
            assert state in VALID_TRANSITIONS, f"Missing transitions for {state}"

    def test_terminal_states_no_outgoing(self):
        for state in TERMINAL_STATES:
            if state in VALID_TRANSITIONS:
                assert len(VALID_TRANSITIONS[state]) == 0

    def test_discovered_to_validating(self):
        assert DealState.VALIDATING in VALID_TRANSITIONS[DealState.DISCOVERED]

    def test_validating_to_buying(self):
        assert DealState.BUYING in VALID_TRANSITIONS[DealState.VALIDATING]

    def test_buying_to_bought(self):
        assert DealState.BOUGHT in VALID_TRANSITIONS[DealState.BUYING]

    def test_invalid_transition_not_in_map(self):
        assert DealState.BOUGHT not in VALID_TRANSITIONS[DealState.DISCOVERED]

    def test_all_states_reachable_from_discovered(self):
        reachable = set()
        queue = [DealState.DISCOVERED]
        visited = set()

        while queue:
            state = queue.pop(0)
            if state in visited:
                continue
            visited.add(state)
            reachable.add(state)
            for next_state in VALID_TRANSITIONS.get(state, set()):
                queue.append(next_state)

        for state in DealState:
            assert state in reachable, f"State {state} unreachable from DISCOVERED"


class TestDealStateValues:
    def test_no_duplicate_values(self):
        values = [s.value for s in DealState]
        assert len(values) == len(set(values))

    def test_states_are_strings(self):
        for state in DealState:
            assert isinstance(state.value, str)


class TestTerminalStates:
    def test_terminal_states_defined(self):
        assert DealState.SOLD in TERMINAL_STATES
        assert DealState.CANCELLED in TERMINAL_STATES
        assert DealState.SOFT_FAILED in TERMINAL_STATES
        assert DealState.HARD_FAILED in TERMINAL_STATES

    def test_non_terminal_states(self):
        assert DealState.DISCOVERED not in TERMINAL_STATES
        assert DealState.BUYING not in TERMINAL_STATES
        assert DealState.LISTED not in TERMINAL_STATES
