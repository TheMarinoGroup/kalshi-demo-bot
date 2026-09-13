import { useCallback, useEffect, useState } from "react";
import type { HudSnapshot } from "./types";

function wsUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/ws`;
}

export function useHud(): {
  snap: HudSnapshot | null;
  live: boolean;
  clock: Date;
  tripKill: () => Promise<void>;
} {
  const [snap, setSnap] = useState<HudSnapshot | null>(null);
  const [live, setLive] = useState(false);
  const [clock, setClock] = useState(() => new Date());

  useEffect(() => {
    const id = window.setInterval(() => setClock(new Date()), 250);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => {
    let closed = false;
    let socket: WebSocket | null = null;
    let poll: number | undefined;

    const apply = (data: HudSnapshot) => {
      if (!closed) setSnap(data);
    };

    const startPoll = () => {
      const tick = async () => {
        try {
          const res = await fetch("/api/snapshot");
          if (res.ok) apply((await res.json()) as HudSnapshot);
        } catch {
          /* desk still booting */
        }
      };
      void tick();
      poll = window.setInterval(() => void tick(), 400);
    };

    const connect = () => {
      socket = new WebSocket(wsUrl());
      socket.onopen = () => setLive(true);
      socket.onmessage = (ev) => apply(JSON.parse(ev.data) as HudSnapshot);
      socket.onclose = () => {
        setLive(false);
        if (!closed) window.setTimeout(connect, 1200);
      };
      socket.onerror = () => socket?.close();
    };

    startPoll();
    connect();
    return () => {
      closed = true;
      if (poll) window.clearInterval(poll);
      socket?.close();
    };
  }, []);

  const tripKill = useCallback(async () => {
    const res = await fetch("/api/kill", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: "manual" }),
    });
    if (res.ok) {
      const body = (await res.json()) as { kill?: HudSnapshot["kill"] };
      setSnap((prev) =>
        prev && body.kill
          ? {
              ...prev,
              kill: body.kill,
            }
          : prev,
      );
    }
  }, []);

  return { snap, live, clock, tripKill };
}
