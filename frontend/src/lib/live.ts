import { useEffect, useRef, useState } from "react";
import { wsUrl } from "./api";
import type { LiveEvent } from "./types";

type Listener = (e: LiveEvent) => void;
const listeners = new Set<Listener>();
let socket: WebSocket | null = null;
let connected = false;
const statusListeners = new Set<(c: boolean) => void>();

let connecting = false;

async function connect() {
  if (socket || connecting) return;
  connecting = true;
  let url: string;
  try {
    url = await wsUrl();
  } catch {
    connecting = false;
    if (listeners.size > 0) setTimeout(() => void connect(), 5000); // nicht angemeldet/offline: später erneut
    return;
  } finally {
    connecting = false;
  }
  if (socket) return;
  socket = new WebSocket(url);
  socket.onopen = () => {
    connected = true;
    statusListeners.forEach((l) => l(true));
  };
  socket.onmessage = (m) => {
    try {
      const e = JSON.parse(m.data) as LiveEvent;
      listeners.forEach((l) => l(e));
    } catch {
      /* ignore */
    }
  };
  socket.onclose = () => {
    socket = null;
    connected = false;
    statusListeners.forEach((l) => l(false));
    if (listeners.size > 0) setTimeout(() => void connect(), 3000);
  };
}

setInterval(() => socket?.readyState === WebSocket.OPEN && socket.send("ping"), 25000);

/** Abonniert Live-Events (WebSocket wird geteilt). */
export function useLive(handler: Listener, types?: string[]) {
  const ref = useRef(handler);
  ref.current = handler;
  const key = types?.join(",");
  useEffect(() => {
    const l: Listener = (e) => {
      if (!types || types.includes(e.type)) ref.current(e);
    };
    listeners.add(l);
    void connect();
    return () => {
      listeners.delete(l);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
}

export function useLiveConnected(): boolean {
  const [c, setC] = useState(connected);
  useEffect(() => {
    statusListeners.add(setC);
    return () => {
      statusListeners.delete(setC);
    };
  }, []);
  return c;
}
