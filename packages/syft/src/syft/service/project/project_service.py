# stdlib

# stdlib
from typing import cast

# relative
from ...serde.serializable import serializable
from ...store.document_store import DocumentStore
from ...store.document_store_errors import NotFoundException
from ...store.document_store_errors import StashException
from ...store.linked_obj import LinkedObject
from ...types.errors import SyftException
from ...types.result import as_result
from ...types.uid import UID
from ...util.telemetry import instrument
from ..context import AuthedServiceContext
from ..network.network_service import NetworkService
from ..notification.notification_service import NotificationService
from ..notification.notifications import CreateNotification
from ..response import SyftError
from ..response import SyftSuccess
from ..service import AbstractService
from ..service import SERVICE_TO_TYPES
from ..service import TYPE_TO_SERVICE
from ..service import service_method
from ..user.user_roles import GUEST_ROLE_LEVEL
from ..user.user_roles import ONLY_DATA_SCIENTIST_ROLE_LEVEL
from ..user.user_roles import ServiceRole
from ..user.user_service import UserService
from .project import Project
from .project import ProjectEvent
from .project import ProjectRequest
from .project import ProjectSubmit
from .project import create_project_hash
from .project_stash import ProjectStash


@instrument
@serializable(canonical_name="ProjectService", version=1)
class ProjectService(AbstractService):
    store: DocumentStore
    stash: ProjectStash

    def __init__(self, store: DocumentStore) -> None:
        self.store = store
        self.stash = ProjectStash(store=store)

    @as_result(SyftException)
    def validate_project_leader(
        self, context: AuthedServiceContext, project: Project
    ) -> None:
        if project.state_sync_leader.verify_key != context.server.verify_key:
            error_msg = "Only the project leader can do this operation"
            raise SyftException(public_message=error_msg)

    @as_result(SyftException)
    def validate_user_permission_for_project(
        self, context: AuthedServiceContext, project: Project
    ) -> None:
        if not project.has_permission(context.credentials):
            error_msg = "User does not have permission to sync events"
            raise SyftException(public_message=error_msg)

    @as_result(StashException)
    def project_exists(
        self, context: AuthedServiceContext, project: ProjectSubmit
    ) -> bool:
        credentials = context.server.verify_key
        try:
            self.stash.get_by_uid(credentials=credentials, uid=project.id).unwrap()
            return True
        except NotFoundException:
            return False

    @as_result(SyftException)
    def validate_project_event_seq(
        self, project_event: ProjectEvent, project: Project
    ) -> None:
        if project_event.seq_no is None:
            raise SyftException(public_message=f"{project_event}.seq_no is None")
        if project_event.seq_no <= len(project.events) and len(project.events) > 0:
            # TODO: We need a way to handle alert returns...
            # e.g. here used to be:
            # SyftNotReady(message="Project out of sync event")
            raise SyftException(public_message="Project events are out of sync")
        if project_event.seq_no > len(project.events) + 1:
            raise SyftException(public_message="Project events are out of order!")

    def is_project_leader(
        self, context: AuthedServiceContext, project: Project
    ) -> bool:
        return context.credentials == project.state_sync_leader.verify_key

    @service_method(
        path="project.can_create_project",
        name="can_create_project",
        roles=ONLY_DATA_SCIENTIST_ROLE_LEVEL,
    )
    def can_create_project(self, context: AuthedServiceContext) -> bool:
        user_service: UserService = context.server.get_service("userservice")  # type: ignore[assignment]
        role = user_service.get_role_for_credentials(
            credentials=context.credentials
        ).unwrap()

        # FIX: Shouldn't it be role >= DATA_SCIENTIST?
        if role == ServiceRole.DATA_SCIENTIST:
            return True
        # TODO: Add Project Errors
        raise SyftException("User cannot create projects")

    @service_method(
        path="project.create_project",
        name="create_project",
        roles=ONLY_DATA_SCIENTIST_ROLE_LEVEL,
        unwrap_on_success=False,
    )
    def create_project(
        self, context: AuthedServiceContext, project: ProjectSubmit
    ) -> SyftSuccess:
        """Start a Project"""
        self.can_create_project(context)

        project_exists = self.project_exists(context, project).unwrap()
        if project_exists:
            raise SyftException(public_message=f"Project {project.id} already exists")

        _project: Project = project.to(Project, context=context)

        # Updating the leader server route of the project object
        # In case the current server, is the leader, they would input their server route
        # For the followers, they would check if the leader is their server peer
        # using the leader's verify_key
        # If the follower do not have the leader as its peer in its routes
        # They would raise as error
        leader_server = _project.state_sync_leader

        # If the current server is a follower
        # For followers the leader server route is retrieved from its peer
        if leader_server.verify_key != context.server.verify_key:
            # FIX: networkservice stash to new BaseStash
            network_service = context.server.get_service("networkservice")
            network_service = cast(NetworkService, network_service)
            peer = network_service.stash.get_by_verify_key(
                credentials=context.server.verify_key,
                verify_key=leader_server.verify_key,
            )
            if peer.is_err():
                this_server_id = context.server.id.short() if context.server.id else ""
                raise SyftException(
                    public_message=(
                        f"Leader Server(id={leader_server.id.short()}) is not a "
                        f"peer of this Server(id={this_server_id})"
                    )
                )
            leader_server_peer = peer.ok()
        else:
            # for the leader server, as it does not have route information to itself
            # we rely on the data scientist to provide the route
            # the route is then validated by the leader
            if project.leader_server_route is not None:
                leader_server_peer = project.leader_server_route.validate_with_context(
                    context=context
                ).unwrap()
            else:
                raise SyftException(
                    message=f"project {project}'s leader_server_route is None"
                )

        _project.leader_server_peer = leader_server_peer
        # This should always be the last call before flushing to DB
        _project.start_hash = create_project_hash(_project)[1]

        stored_project: Project = self.stash.set(context.credentials, _project).unwrap()
        stored_project = self.add_signing_key_to_project(context, stored_project)

        return SyftSuccess(message="Project successfully created", value=stored_project)

    @service_method(
        path="project.add_event",
        name="add_event",
        roles=GUEST_ROLE_LEVEL,
        unwrap_on_success=False,
    )
    def add_event(
        self, context: AuthedServiceContext, project_event: ProjectEvent
    ) -> SyftSuccess:
        """Add events to a project"""
        # Event object should be received from the leader of the project
        if not isinstance(project_event, ProjectEvent):
            raise SyftException(
                public_message="project_event should be a ProjectEvent object"
            )

        project = self.stash.get_by_uid(
            context.server.verify_key, uid=project_event.project_id
        ).unwrap()

        # FIX: MERGE: Rename function below
        self.validate_project_leader(context, project).unwrap(
            public_message="Project Events should be passed to leader by broadcast endpoint"
        )

        if not self.is_project_leader(context, project):
            raise SyftException(
                public_message="Only the leader of the project can add events"
            )

        project.events.append(project_event)
        project.event_id_hashmap[project_event.id] = project_event

        # TODO: better name for the function should be check_and_notify or something?
        self.check_for_project_request(project, project_event, context)

        updated_project = self.stash.update(context.server.verify_key, project).unwrap()

        return SyftSuccess(
            message=f"Project event {project_event.id} added successfully",
            value=updated_project,
        )

    @service_method(
        path="project.broadcast_event",
        name="broadcast_event",
        roles=GUEST_ROLE_LEVEL,
        unwrap_on_success=False,
    )
    def broadcast_event(
        self, context: AuthedServiceContext, project_event: ProjectEvent
    ) -> SyftSuccess:
        """To add events to a projects"""
        # Only the leader of the project could add events to the projects
        # Any Event to be added to the project should be sent to the leader of the project
        # The leader broadcasts the event to all the members of the project

        project = self.stash.get_by_uid(
            context.server.verify_key, uid=project_event.project_id
        ).unwrap()

        self.validate_project_leader(context, project).unwrap(
            public_message="Only the leader of the project can broadcast events"
        )
        self.validate_user_permission_for_project(context, project)
        self.validate_project_event_seq(project_event, project).unwrap()

        project.events.append(project_event)
        project.event_id_hashmap[project_event.id] = project_event

        self.check_for_project_request(project, project_event, context)

        # Broadcast the event to all the members of the project
        network_service = context.server.get_service("networkservice")
        for member in project.members:
            if member.verify_key != context.server.verify_key:
                # Retrieving the ServerPeer Object to communicate with the server
                peer = network_service.stash.get_by_verify_key(
                    credentials=context.server.verify_key,
                    verify_key=member.verify_key,
                )

                if peer.is_err():
                    raise SyftException(
                        public_message=f"Leader server does not have peer {member.name}-{member.id.short()}"
                        + ". Please exchange routes with the peer."
                    )
                peer = peer.ok()
                remote_client = peer.client_with_context(context=context)
                if remote_client.is_err():
                    raise SyftException(
                        public_message=f"Failed to create remote client for peer: "
                        f"{peer.id}. Error: {remote_client.err()}"
                    )
                remote_client = remote_client.ok()
                remote_client.api.services.project.add_event(project_event)

        updated_project = self.stash.update(context.server.verify_key, project).unwrap()

        return SyftSuccess(
            message=f"Event #{project_event.seq_no} of {project.name} broadcasted successfully",
            value=updated_project,
        )

    @service_method(
        path="project.sync",
        name="sync",
        roles=GUEST_ROLE_LEVEL,
    )
    def sync(
        self, context: AuthedServiceContext, project_id: UID, seq_no: int
    ) -> list[ProjectEvent]:
        """Given a starting event seq_no, gets all following events from a project"""
        if seq_no < 0:
            raise SyftException(
                public_message="Input seq_no should be a non negative integer"
            )

        # Event object should be received from the leader of the project
        project = self.stash.get_by_uid(
            context.server.verify_key, uid=project_id
        ).unwrap()

        self.validate_project_leader(context, project)
        self.validate_user_permission_for_project(context, project)

        return project.events[seq_no:]

    @service_method(path="project.get_all", name="get_all", roles=GUEST_ROLE_LEVEL)
    def get_all(self, context: AuthedServiceContext) -> list[Project]:
        projects: list[Project] = self.stash.get_all(context.credentials).unwrap()

        for idx, project in enumerate(projects):
            projects[idx] = self.add_signing_key_to_project(context, project)

        return projects

    @service_method(
        path="project.get_by_name",
        name="get_by_name",
        roles=GUEST_ROLE_LEVEL,
    )
    def get_by_name(self, context: AuthedServiceContext, name: str) -> Project:
        try:
            project = self.stash.get_by_name(
                context.credentials, project_name=name
            ).unwrap()
        except NotFoundException as exc:
            raise NotFoundException.from_exception(
                exc, public_message="Project '{name}' does not exist"
            )

        return self.add_signing_key_to_project(context, project)

    @service_method(
        path="project.get_by_uid",
        name="get_by_uid",
        roles=GUEST_ROLE_LEVEL,
    )
    def get_by_uid(self, context: AuthedServiceContext, uid: UID) -> Project:
        try:
            credentials = context.server.verify_key
            return self.stash.get_by_uid(credentials=credentials, uid=uid).unwrap()
        except NotFoundException as exc:
            raise NotFoundException.from_exception(
                exc, public_message=f"Project {uid} not found"
            )

    as_result(StashException, NotFoundException)

    def add_signing_key_to_project(
        self, context: AuthedServiceContext, project: Project
    ) -> Project:
        user_service = context.server.get_service("userservice")
        try:
            user = user_service.stash.get_by_verify_key(
                credentials=context.credentials, verify_key=context.credentials
            ).unwrap()
        except NotFoundException as exc:
            raise NotFoundException.from_exception(
                exc, public_message="User not found! Please register the user first"
            )
        # Automatically infuse signing key of user
        project.user_signing_key = user.signing_key

        return project

    # TODO: Glob Notification error here
    @as_result(SyftException)
    def check_for_project_request(
        self,
        project: Project,
        project_event: ProjectEvent,
        context: AuthedServiceContext,
    ) -> None:
        # TODO: Should we really raise an exception if notification fails to be sent?
        #       Maybe logging and moving on is better?
        """
        Checks if there are any ProjectEvent requests and messages the admin
        in case there is one.

        This method raises an exception if the notification fails to send.

        Args:
            project (Project): Project object
            project_event (ProjectEvent): Project event object
            context (AuthedServiceContext): Context of the server

        Returns:
            SyftSuccess: SyftSuccess if message is created else SyftError
        """
        if (
            isinstance(project_event, ProjectRequest)
            and project_event.linked_request.server_uid == context.server.id
        ):
            link = LinkedObject.with_context(project, context=context)

            message = CreateNotification(
                subject=f"A new request has been added to the project {project.name}",
                from_user_verify_key=context.credentials,
                to_user_verify_key=context.server.verify_key,
                linked_obj=link,
            )

            # TODO: Update noteificationservice result
            method = context.server.get_service_method(NotificationService.send)
            result = method(context=context, notification=message)
            if isinstance(result, SyftError):
                raise SyftException(public_message=result)


TYPE_TO_SERVICE[Project] = ProjectService
SERVICE_TO_TYPES[ProjectService].update({Project})
