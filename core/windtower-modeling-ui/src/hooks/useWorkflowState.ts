import { useCallback, useEffect, useState } from "react";

import { getWorkflowState } from "../api";
import type { WorkflowState } from "../types";

export function useWorkflowState(projectId: string | null) {
  const [workflowState, setWorkflowState] = useState<WorkflowState | null>(null);
  const [workflowLoading, setWorkflowLoading] = useState(false);

  const refreshWorkflow = useCallback(async () => {
    if (!projectId) {
      setWorkflowState(null);
      return null;
    }
    setWorkflowLoading(true);
    try {
      const state = await getWorkflowState(projectId);
      setWorkflowState(state);
      return state;
    } finally {
      setWorkflowLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void refreshWorkflow();
  }, [refreshWorkflow]);

  return { workflowState, workflowLoading, refreshWorkflow };
}
