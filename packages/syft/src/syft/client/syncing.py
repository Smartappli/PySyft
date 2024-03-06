# stdlib
from time import sleep
from typing import List
from typing import Optional

# relative
from ..service.action.action_permissions import ActionObjectPermission
from ..service.action.action_permissions import ActionPermission
from ..service.code.user_code import UserCode
from ..service.job.job_stash import Job
from ..service.sync.diff_state import NodeDiff
from ..service.sync.diff_state import ObjectDiff
from ..service.sync.diff_state import ObjectDiffBatch
from ..service.sync.diff_state import ResolvedSyncState
from ..service.sync.diff_state import SyncDecision
from ..service.sync.sync_state import SyncState


def compare_states(low_state: SyncState, high_state: SyncState) -> NodeDiff:
    return NodeDiff.from_sync_state(low_state=low_state, high_state=high_state)


def get_user_input_for_resolve() -> Optional[str]:
    print(
        "Do you want to keep the low state or the high state for these objects? choose 'low' or 'high'"
    )

    while True:
        decision = input()
        decision = decision.lower()

        if decision in ["low", "high"]:
            return decision
        else:
            print("Please choose between `low` or `high`")


def resolve(
    state: NodeDiff, decision: Optional[str] = None, share_private_objects: bool = False
) -> tuple[ResolvedSyncState, ResolvedSyncState]:
    # TODO: only add permissions for objects where we manually give permission
    # Maybe default read permission for some objects (high -> low)
    resolved_state_low: ResolvedSyncState = ResolvedSyncState(alias="low")
    resolved_state_high: ResolvedSyncState = ResolvedSyncState(alias="high")

    for batch_diff in state.hierarchies:
        batch_decision = decision
        if all(diff.status == "SAME" for diff in batch_diff.diffs):
            # Hierarchy has no diffs
            continue

        print(batch_diff.__repr__())

        # ask question: which side do you want
        # ask question: The batch has private items that you may want to share with the related user
        # user with verify key: abc. The items are
        # Log with id (123)
        # Result with id (567)
        # do you want to give read permission to items
        # TODO: get decision
        # get items
        if batch_decision is None:
            batch_decision = get_user_input_for_resolve()

        sync_decisions: list[SyncDecision] = get_sync_decisions_for_batch_items(
            batch_diff,
            batch_decision,
            share_private_objects=share_private_objects,
        )

        print(f"Decision: Syncing {len(batch_diff)} objects from {batch_decision} side")

        for sync_decision in sync_decisions:
            resolved_state_low.add_sync_decision(sync_decision)
            resolved_state_high.add_sync_decision(sync_decision)

        print()
        print("=" * 100)
        print()

    return resolved_state_low, resolved_state_high


def get_sync_decisions_for_batch_items(
    batch_diff: ObjectDiffBatch,
    decision: str,
    share_private_objects: bool = False,
) -> list[SyncDecision]:
    sync_decisions: list[SyncDecision] = []

    unpublished_private_high_diffs: list[ObjectDiff] = []
    for diff in batch_diff.diffs:
        if diff.high_obj is not None and diff.high_obj._has_private_sync_attrs():
            # TODO check for existing storage permissions
            unpublished_private_high_diffs.append(diff)

    user_codes_high: List[UserCode] = [
        diff.high_obj
        for diff in batch_diff.diffs
        if isinstance(diff.high_obj, UserCode)
    ]
    if len(user_codes_high) > 1:
        raise ValueError("too many user codes")
    if len(user_codes_high) == 0:
        user_code_high = None
    else:
        user_code_high = user_codes_high[0]

    if user_code_high is None and len(unpublished_private_high_diffs):
        raise ValueError("Found unpublished private objects without user code")

    if share_private_objects:
        private_high_diffs_to_share = unpublished_private_high_diffs
    else:
        private_high_diffs_to_share = ask_user_input_permission(
            user_code_high, unpublished_private_high_diffs
        )

    for diff in batch_diff.diffs:
        is_unpublished_private_diff = diff in unpublished_private_high_diffs
        has_share_decision = diff in private_high_diffs_to_share

        if isinstance(diff.high_obj, Job):
            if user_code_high is None:
                raise ValueError("Job without user code")
            # Jobs are always shared
            # TODO make job result cache empty
            new_permissions_low_side = [
                ActionObjectPermission(
                    uid=diff.object_id,
                    permission=ActionPermission.READ,
                    credentials=user_code_high.user_verify_key,
                )
            ]
            mockify = False

        elif is_unpublished_private_diff and has_share_decision:
            # private + want to share
            new_permissions_low_side = [
                ActionObjectPermission(
                    uid=diff.object_id,
                    permission=ActionPermission.READ,
                    credentials=user_code_high.user_verify_key,
                )
            ]
            mockify = False

        elif is_unpublished_private_diff and not has_share_decision:
            # private + do not share
            new_permissions_low_side = []
            mockify = True

        else:
            # any other object is shared
            new_permissions_low_side = []
            mockify = False

        sync_decisions.append(
            SyncDecision(
                diff=diff,
                decision=decision,
                new_permissions_lowside=new_permissions_low_side,
                mockify=mockify,
            )
        )

    return sync_decisions


QUESTION_SHARE_PRIVATE_OBJS = """You currently have the following private objects:

{objects_str}

Do you want to share some of these private objects? If so type the first 3 characters of the id e.g. 'abc'.
If you dont want to share any more private objects, type "no"
"""

CONFIRMATION_SHARE_PRIVATE_OBJ = """Setting permissions for {object_type} #{object_id} to share with {user_verify_key},
this will become effective when you call client.apply_state(<resolved_state>))
"""


def ask_user_input_permission(user_code, all_private_high_diffs) -> list[ObjectDiff]:
    if len(all_private_high_diffs) == 0:
        return []

    user_verify_key = user_code.user_verify_key
    private_high_diffs_to_share = []
    print(
        f"""This batch of updates contains new private objects on the high side that you may want \
    to share with user {user_verify_key}."""
    )

    remaining_private_high_diffs = all_private_high_diffs[:]
    while len(remaining_private_high_diffs):
        objects_str = "\n".join(
            [
                f"{diff.object_type} #{diff.object_id}"
                for diff in remaining_private_high_diffs
            ]
        )
        print(QUESTION_SHARE_PRIVATE_OBJS.format(objects_str=objects_str), flush=True)

        sleep(0.1)
        res = input()
        if res == "no":
            break
        elif len(res) >= 3:
            matches = [
                diff
                for diff in remaining_private_high_diffs
                if str(diff.object_id).startswith(res)
            ]
            if len(matches) == 0:
                print("Invalid input")
                continue
            elif len(matches) == 1:
                diff = matches[0]
                print()
                print("=" * 100)
                print()
                print(
                    CONFIRMATION_SHARE_PRIVATE_OBJ.format(
                        object_type=diff.object_type,
                        object_id=diff.object_id,
                        user_verify_key=user_verify_key,
                    )
                )

                remaining_private_high_diffs.remove(diff)
                private_high_diffs_to_share.append(diff)
                # new_permissions_lowside.append(
                #     ActionObjectPermission(
                #         uid=diff.object_id,
                #         permission=ActionPermission.READ,
                #         credentials=user_code_high.user_verify_key,
                #     )
                # )
                # questions
                # Q:do we also want to give read permission if we defined that by accept_by_depositing_result?
                # A:only if we pass: sync_read_permission to resolve
            else:
                print("Found multiple matches for provided id, exiting")
                break
        else:
            print("invalid input")

    return private_high_diffs_to_share
