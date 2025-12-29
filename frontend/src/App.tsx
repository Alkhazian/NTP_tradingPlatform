import Dashboard from './components/Dashboard'
import { TradingProvider } from './context/TradingContext'

function App() {
  return (
    <TradingProvider>
      <Dashboard />
    </TradingProvider>
  )
}

export default App
