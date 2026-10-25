import { useCallback, useEffect, useRef, useState } from 'react';
import type { DependencyList } from 'react';
import { errorMessage } from './api';

export function useResource<T>(loader: () => Promise<T>, deps: DependencyList) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [version, setVersion] = useState(0);
  const reload = useCallback(() => setVersion((value) => value + 1), []);
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError('');
    Promise.resolve()
      .then(loader)
      .then((value) => {
        if (active) setData(value);
      })
      .catch((reason) => {
        if (active) setError(errorMessage(reason));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [...deps, version]);
  return { data, error, loading, reload, version };
}

export function useAction(onError: (message: string) => void) {
  const [pending, setPending] = useState(false);
  const lock = useRef(false);
  async function run(action: () => Promise<void>) {
    if (lock.current) return;
    lock.current = true;
    setPending(true);
    try {
      await action();
    } catch (error) {
      onError(errorMessage(error));
    } finally {
      lock.current = false;
      setPending(false);
    }
  }
  return { run, pending };
}
