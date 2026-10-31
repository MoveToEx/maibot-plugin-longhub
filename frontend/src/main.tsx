import { Component, StrictMode } from 'react';
import type { ErrorInfo, ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { HashRouter, MemoryRouter } from 'react-router';
import App from './App';
import { Button } from './components/ui';
import './styles.css';

class ErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('LONG Hub panel', error, info.componentStack);
  }
  render() {
    return this.state.failed ? (
      <div className="bootstrap">
        <h1 className="text-xl font-semibold">面板暂时遇到问题</h1>
        <p className="text-muted my-5">请重新加载页面后重试。</p>
        <Button variant="primary" onClick={() => window.location.reload()}>
          重新加载
        </Button>
      </div>
    ) : (
      this.props.children
    );
  }
}

// MaiBot uses an opaque-origin srcdoc iframe. HashRouter cannot build URLs
// relative to about:srcdoc; keep this iframe's navigation in React Router memory.
const Router = window.maibot && location.protocol === 'about:' ? MemoryRouter : HashRouter;

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <Router>
        <App />
      </Router>
    </ErrorBoundary>
  </StrictMode>,
);
