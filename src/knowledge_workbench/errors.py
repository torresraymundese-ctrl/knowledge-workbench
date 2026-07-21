class KnowledgeWorkbenchError(Exception):
    """Base error shown to CLI users without a traceback."""


class UnsupportedFormatError(KnowledgeWorkbenchError):
    pass


class MissingDependencyError(KnowledgeWorkbenchError):
    pass


class PolicyDeniedError(KnowledgeWorkbenchError):
    pass


class InvalidTransitionError(KnowledgeWorkbenchError):
    pass

