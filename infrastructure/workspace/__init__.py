from infrastructure.workspace.setup import WorkspaceBuilder
from infrastructure.workspace.resume import ResumeWorkspace, WorkspaceResumeResolver

setup_workspace = WorkspaceBuilder.setup_workspace

__all__ = ['ResumeWorkspace', 'WorkspaceBuilder', 'WorkspaceResumeResolver', 'setup_workspace']
