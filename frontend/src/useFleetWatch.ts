import { useCallback, useEffect, useRef, useState } from "react";

import {
  FALLBACK_SITE,
  postJsonVersioned,
  requestDashboard,
  requestHistory,
  requestSite,
} from "./api";
import type {
  ConnectionState,
  DashboardSnapshot,
  RobotHistory,
  SiteConfig,
} from "./types";

interface DashboardFeed {
  snapshot: DashboardSnapshot | null;
  site: SiteConfig;
  siteIsFallback: boolean;
  connection: ConnectionState;
  error: string | null;
  refresh: () => void;
  clearError: () => void;
}

function message(reason: unknown): string {
  return reason instanceof Error ? reason.message : "Fleet data is unavailable.";
}

export function useDashboardFeed(): DashboardFeed {
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null);
  const [site, setSite] = useState(FALLBACK_SITE);
  const [siteIsFallback, setSiteIsFallback] = useState(true);
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [error, setError] = useState<string | null>(null);
  const pending = useRef(false);
  const activeRequest = useRef<AbortController | null>(null);

  const loadDashboard = useCallback(async () => {
    if (pending.current || document.visibilityState === "hidden") return;
    pending.current = true;
    const controller = new AbortController();
    activeRequest.current = controller;
    try {
      setSnapshot(await requestDashboard(controller.signal));
      setConnection("connected");
      setError(null);
    } catch (reason) {
      if (!controller.signal.aborted) {
        setConnection("offline");
        setError(message(reason));
      }
    } finally {
      if (activeRequest.current === controller) activeRequest.current = null;
      pending.current = false;
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void requestSite(controller.signal)
      .then((nextSite) => {
        setSite(nextSite);
        setSiteIsFallback(false);
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  useEffect(() => {
    void loadDashboard();
    const timer = window.setInterval(() => void loadDashboard(), 3_000);
    const onVisible = (): void => {
      if (document.visibilityState === "visible") void loadDashboard();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
      activeRequest.current?.abort();
    };
  }, [loadDashboard]);

  return {
    snapshot,
    site,
    siteIsFallback,
    connection,
    error,
    refresh: () => void loadDashboard(),
    clearError: () => setError(null),
  };
}

export interface RobotHistoryState {
  history: RobotHistory | null;
  loading: boolean;
  error: string | null;
  retry: () => void;
}

export function useRobotHistory(
  robotId: string | null,
  revision: string | null,
): RobotHistoryState {
  const [history, setHistory] = useState<RobotHistory | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    if (robotId === null) {
      setHistory(null);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void requestHistory(robotId, controller.signal)
      .then(setHistory)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(message(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [retry, revision, robotId]);

  return { history, loading, error, retry: () => setRetry((value) => value + 1) };
}

export async function runVersionedAction(
  path: string,
  revision: string,
  body?: Record<string, unknown>,
): Promise<void> {
  await postJsonVersioned(path, revision, body);
}
