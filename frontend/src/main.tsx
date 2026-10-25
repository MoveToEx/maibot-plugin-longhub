import { Component, StrictMode } from 'react';
import type { ErrorInfo, ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { HashRouter } from 'react-router';
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

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <HashRouter>
        <App />
      </HashRouter>
    </ErrorBoundary>
  </StrictMode>,
);
