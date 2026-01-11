import React, { useState, useEffect } from 'react';
import { Card, CardHeader, CardTitle, CardContent } from './ui/card';
import { Badge } from './ui/badge';

interface Strategy {
    id: string;
    running: boolean;
    status: string;
    config: {
        id: string;
        name: string;
        strategy_type: string;
        enabled: boolean;
        instrument_id: string;
        order_size: number;
        parameters?: any;
    };
    metrics?: {
        total_trades: number;
        win_rate: number;
        total_pnl: number;
        total_commission: number;
        net_pnl: number;
    };
}

interface BacktestResult {
    run_id: string;
    strategy_id: string;
    start_date: string;
    end_date: string;
    instruments: string[];
    total_trades: number;
    statistics?: {
        pnls?: any;
        returns?: any;
        general?: any;
    };
}

const Backtesting: React.FC = () => {
    const [strategies, setStrategies] = useState<Strategy[]>([]);
    const [selectedStrategies, setSelectedStrategies] = useState<string[]>([]);
    const [backtestResults, setBacktestResults] = useState<BacktestResult[]>([]);
    const [loading, setLoading] = useState(false);
    const [availableData, setAvailableData] = useState<{ [key: string]: any }>({});
    const [ingestStatus, setIngestStatus] = useState<{
        is_ingesting: boolean;
        last_result: any;
        current_file: string | null;
        error: string | null;
    }>({
        is_ingesting: false,
        last_result: null,
        current_file: null,
        error: null
    });

    const [startDate, setStartDate] = useState('2023-01-01');
    const [endDate, setEndDate] = useState('2023-12-31');
    const [initialBalance, setInitialBalance] = useState(100000);
    const [currency, setCurrency] = useState('USD');
    const [selectedInstruments] = useState<string[]>(['ES.FUT']);

    useEffect(() => {
        fetchStrategies();
        fetchAvailableData();
        fetchBacktestResults();
    }, []);

    const fetchStrategies = async () => {
        try {
            const response = await fetch('/strategies');
            const data = await response.json();
            setStrategies(data);
        } catch (error) {
            console.error('Error fetching strategies:', error);
        }
    };

    const fetchAvailableData = async () => {
        try {
            const response = await fetch('/backtest/available-data');
            const data = await response.json();
            setAvailableData(data);
        } catch (error) {
            console.error('Error fetching available data:', error);
        }
    };

    const fetchBacktestResults = async () => {
        try {
            const response = await fetch('/backtest/results');
            const data = await response.json();
            setBacktestResults(data);
        } catch (error) {
            console.error('Error fetching backtest results:', error);
        }
    };

    const fetchIngestStatus = async () => {
        try {
            const response = await fetch('/backtest/ingest-status');
            const data = await response.json();
            setIngestStatus(data);

            if (!data.is_ingesting && data.last_result && !ingestStatus.last_result) {
                // Just finished
                fetchAvailableData();
            }
        } catch (error) {
            console.error('Error fetching ingest status:', error);
        }
    };

    useEffect(() => {
        let interval: any;
        if (ingestStatus.is_ingesting) {
            interval = setInterval(fetchIngestStatus, 2000);
        }
        return () => {
            if (interval) clearInterval(interval);
        };
    }, [ingestStatus.is_ingesting]);

    const handleStrategyToggle = (strategyId: string) => {
        setSelectedStrategies(prev =>
            prev.includes(strategyId)
                ? prev.filter(id => id !== strategyId)
                : [...prev, strategyId]
        );
    };

    const runBacktest = async (strategyId: string) => {
        const commission = (document.getElementById('backtest-commission') as HTMLInputElement)?.value;
        const slippage = (document.getElementById('backtest-slippage') as HTMLInputElement)?.value;

        setLoading(true);
        try {
            const strategy = strategies.find(s => s.id === strategyId);
            if (!strategy) return;

            const response = await fetch('/backtest/run', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    strategy_id: strategyId,
                    strategy_config: {
                        id: strategyId,
                        name: strategy.config.name,
                        strategy_type: strategy.config.strategy_type,
                        instrument_id: selectedInstruments[0],
                        order_size: 1,
                        parameters: {},
                        commission_per_contract: commission ? parseFloat(commission) : 0,
                        slippage_prob: slippage ? parseFloat(slippage) : 0
                    },
                    instruments: selectedInstruments,
                    start_date: startDate,
                    end_date: endDate,
                    venue: 'SIM',
                    initial_balance: initialBalance,
                    currency: currency
                })
            });

            if (response.ok) {
                const result = await response.json();
                alert(`Backtest started! Run ID: ${result.run_id} `);
                fetchBacktestResults();
            } else {
                const error = await response.text();
                alert(`Error: ${error} `);
            }
        } catch (error) {
            console.error('Error running backtest:', error);
            alert('Failed to run backtest');
        } finally {
            setLoading(false);
        }
    };

    const runMultipleBacktests = async () => {
        for (const strategyId of selectedStrategies) {
            await runBacktest(strategyId);
        }
    };

    const downloadTearsheet = (runId: string) => {
        window.open(`/ backtest / tearsheet / ${runId} `, '_blank');
    };

    const downloadTradesCSV = (runId: string) => {
        window.open(`/ backtest / trades - csv / ${runId} `, '_blank');
    };

    const calculateYearlyPnL = (result: BacktestResult) => {
        const startYear = new Date(result.start_date).getFullYear();
        const endYear = new Date(result.end_date).getFullYear();
        const totalPnL = result.statistics?.pnls?.Total || 0;
        const years = endYear - startYear + 1;

        const yearlyData: { [key: number]: number } = {};
        for (let year = startYear; year <= endYear; year++) {
            yearlyData[year] = totalPnL / years;
        }

        return yearlyData;
    };

    const ingestSingleFile = async () => {
        const instrumentId = (document.getElementById('instrument-id') as HTMLInputElement).value;
        const filePath = (document.getElementById('file-path') as HTMLInputElement).value;
        const timezone = (document.getElementById('timezone') as HTMLSelectElement).value;
        const barTypeSelect = (document.getElementById('bar-type') as HTMLSelectElement).value;
        const customBarType = (document.getElementById('custom-bar-type') as HTMLInputElement)?.value;

        const barType = barTypeSelect === 'custom' ? customBarType : barTypeSelect;

        if (!instrumentId || !filePath) {
            alert('Please fill in all fields');
            return;
        }

        if (barTypeSelect === 'custom' && !customBarType) {
            alert('Please enter a custom bar type');
            return;
        }

        try {
            const response = await fetch('/backtest/ingest', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    file_path: filePath,
                    instrument_id: instrumentId,
                    timezone: timezone,
                    bar_type: barType,
                    venue: 'SIM'
                })
            });

            if (response.status === 202) {
                setIngestStatus(prev => ({ ...prev, is_ingesting: true }));
                fetchIngestStatus(); // Start polling immediately
            } else {
                const error = await response.text();
                alert(`Error: ${error} `);
            }
        } catch (error) {
            console.error('Ingestion error:', error);
            alert('Failed to ingest data');
        }
    };

    const ingestDirectory = async () => {
        const directory = (document.getElementById('directory') as HTMLInputElement).value;
        const mappingStr = (document.getElementById('mapping') as HTMLInputElement).value;
        const timezone2 = (document.getElementById('timezone-2') as HTMLSelectElement).value;
        const barType2 = (document.getElementById('bar-type-2') as HTMLSelectElement).value;
        const customBarType2 = (document.getElementById('custom-bar-type-2') as HTMLInputElement)?.value;

        const barType = barType2 === 'custom' ? customBarType2 : barType2;

        if (!directory || !mappingStr) {
            alert('Please fill in all fields');
            return;
        }

        if (barType2 === 'custom' && !customBarType2) {
            alert('Please enter a custom bar type');
            return;
        }

        try {
            const mapping = JSON.parse(mappingStr);
            const response = await fetch('/backtest/ingest', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    directory: directory,
                    instrument_mapping: mapping,
                    timezone: timezone2,
                    bar_type: barType,
                    venue: 'SIM'
                })
            });

            if (response.status === 202) {
                setIngestStatus(prev => ({ ...prev, is_ingesting: true }));
                fetchIngestStatus(); // Start polling immediately
            } else {
                const error = await response.text();
                alert(`Error: ${error} `);
            }
        } catch (error) {
            console.error('Ingestion error:', error);
            alert('Failed to ingest data. Check the pattern format.');
        }
    };

    return (
        <div className="space-y-6">
            {/* Data Ingestion Section */}
            <Card variant="glass">
                <CardHeader>
                    <CardTitle>📁 Data Management</CardTitle>
                    <p className="text-sm text-muted-foreground mt-1">
                        <strong>Note:</strong> Data needs to be ingested only once. After ingestion, it's available for all backtests.
                    </p>
                </CardHeader>
                <CardContent>
                    {/* Ingestion Status Alerts */}
                    {ingestStatus.is_ingesting && (
                        <div className="mb-6 p-4 bg-cyan-900/40 border border-cyan-500/50 rounded-xl flex items-center gap-4 animate-pulse">
                            <div className="w-5 h-5 border-2 border-cyan-400 border-t-transparent rounded-full animate-spin"></div>
                            <div>
                                <p className="text-cyan-100 font-medium">Ingestion in progress...</p>
                                <p className="text-cyan-300/80 text-sm">Processing: {ingestStatus.current_file}</p>
                            </div>
                        </div>
                    )}

                    {!ingestStatus.is_ingesting && ingestStatus.error && (
                        <div className="mb-6 p-4 bg-red-900/40 border border-red-500/50 rounded-xl flex items-center gap-4">
                            <div className="text-red-400">⚠️</div>
                            <div>
                                <p className="text-red-100 font-medium">Ingestion failed</p>
                                <p className="text-red-300/80 text-sm">{ingestStatus.error}</p>
                            </div>
                            <button onClick={() => setIngestStatus(prev => ({ ...prev, error: null }))} className="ml-auto text-red-400 hover:text-red-300">✕</button>
                        </div>
                    )}

                    {!ingestStatus.is_ingesting && ingestStatus.last_result && (
                        <div className="mb-6 p-4 bg-emerald-900/40 border border-emerald-500/50 rounded-xl flex items-center gap-4">
                            <div className="text-emerald-400">✅</div>
                            <div>
                                <p className="text-emerald-100 font-medium">Ingestion successful</p>
                                <p className="text-emerald-300/80 text-sm">
                                    {ingestStatus.last_result.bars_ingested ?
                                        `Ingested ${ingestStatus.last_result.bars_ingested.toLocaleString()} bars.` :
                                        `Completed directory ingestion.`}
                                </p>
                            </div>
                            <button onClick={() => setIngestStatus(prev => ({ ...prev, last_result: null }))} className="ml-auto text-emerald-400 hover:text-emerald-300">✕</button>
                        </div>
                    )}

                    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                        {/* Method 1 */}
                        <div className="p-4 rounded-xl bg-white/5 border border-white/10">
                            <h3 className="text-lg font-semibold mb-2">Method 1: Single File</h3>
                            <p className="text-sm text-muted-foreground mb-4">
                                Place Parquet files in <code className="px-2 py-1 bg-white/10 rounded">data/historical_data/</code>
                            </p>

                            <div className="space-y-3">
                                <div>
                                    <label className="text-sm text-muted-foreground">Instrument ID</label>
                                    <input type="text" placeholder="e.g., ES.FUT" id="instrument-id"
                                        className="w-full mt-1 px-3 py-2 bg-white/5 border border-white/10 rounded-lg text-sm focus:outline-none focus:border-cyan-500" />
                                </div>

                                <div>
                                    <label className="text-sm text-muted-foreground">Relative Path</label>
                                    <input type="text" placeholder="e.g., ES/ES_2023.parquet" id="file-path"
                                        className="w-full mt-1 px-3 py-2 bg-white/5 border border-white/10 rounded-lg text-sm focus:outline-none focus:border-cyan-500" />
                                </div>

                                <div>
                                    <label className="text-sm text-muted-foreground">Bar Type</label>
                                    <select id="bar-type" onChange={(e) => {
                                        const customField = document.getElementById('custom-bar-type-field');
                                        if (customField) customField.style.display = e.target.value === 'custom' ? 'block' : 'none';
                                    }} className="w-full mt-1 px-3 py-2 bg-white/5 border border-white/10 rounded-lg text-sm focus:outline-none focus:border-cyan-500">
                                        <option value="1-SECOND-LAST">1-Second</option>
                                        <option value="3-SECOND-LAST">3-Second</option>
                                        <option value="5-SECOND-LAST">5-Second</option>
                                        <option value="10-SECOND-LAST">10-Second</option>
                                        <option value="30-SECOND-LAST">30-Second</option>
                                        <option value="1-MINUTE-LAST" selected>1-Minute</option>
                                        <option value="5-MINUTE-LAST">5-Minute</option>
                                        <option value="15-MINUTE-LAST">15-Minute</option>
                                        <option value="30-MINUTE-LAST">30-Minute</option>
                                        <option value="1-HOUR-LAST">1-Hour</option>
                                        <option value="custom">Custom</option>
                                    </select>
                                </div>

                                <div id="custom-bar-type-field" style={{ display: 'none' }}>
                                    <label className="text-sm text-muted-foreground">Custom Bar Type</label>
                                    <input type="text" placeholder="e.g., 2-SECOND-LAST" id="custom-bar-type"
                                        className="w-full mt-1 px-3 py-2 bg-white/5 border border-white/10 rounded-lg text-sm focus:outline-none focus:border-cyan-500" />
                                </div>

                                <div>
                                    <label className="text-sm text-muted-foreground">Timezone</label>
                                    <select id="timezone" className="w-full mt-1 px-3 py-2 bg-white/5 border border-white/10 rounded-lg text-sm focus:outline-none focus:border-cyan-500">
                                        <option value="UTC">UTC</option>
                                        <option value="US/Eastern">US/Eastern</option>
                                        <option value="Europe/London">Europe/London</option>
                                        <option value="Asia/Tokyo">Asia/Tokyo</option>
                                    </select>
                                </div>

                                <button onClick={ingestSingleFile}
                                    className="w-full px-4 py-2 bg-cyan-600 hover:bg-cyan-700 text-white rounded-lg font-medium transition-colors">
                                    Ingest Single File
                                </button>
                            </div>
                        </div>

                        {/* Method 2 */}
                        <div className="p-4 rounded-xl bg-white/5 border border-white/10">
                            <h3 className="text-lg font-semibold mb-2">Method 2: Batch Directory</h3>
                            <p className="text-sm text-muted-foreground mb-4">
                                Ingest all files from a directory using pattern matching
                            </p>

                            <div className="space-y-3">
                                <div>
                                    <label className="text-sm text-muted-foreground">Directory</label>
                                    <input type="text" placeholder="e.g., ES" id="directory"
                                        className="w-full mt-1 px-3 py-2 bg-white/5 border border-white/10 rounded-lg text-sm focus:outline-none focus:border-cyan-500" />
                                </div>

                                <div>
                                    <label className="text-sm text-muted-foreground">Pattern → Instrument</label>
                                    <input type="text" placeholder='{"*.parquet": "ES.FUT"}' id="mapping"
                                        className="w-full mt-1 px-3 py-2 bg-white/5 border border-white/10 rounded-lg text-sm focus:outline-none focus:border-cyan-500" />
                                </div>

                                <div>
                                    <label className="text-sm text-muted-foreground">Bar Type</label>
                                    <select id="bar-type-2" onChange={(e) => {
                                        const customField = document.getElementById('custom-bar-type-field-2');
                                        if (customField) customField.style.display = e.target.value === 'custom' ? 'block' : 'none';
                                    }} className="w-full mt-1 px-3 py-2 bg-white/5 border border-white/10 rounded-lg text-sm focus:outline-none focus:border-cyan-500">
                                        <option value="1-SECOND-LAST">1-Second</option>
                                        <option value="3-SECOND-LAST">3-Second</option>
                                        <option value="5-SECOND-LAST">5-Second</option>
                                        <option value="10-SECOND-LAST">10-Second</option>
                                        <option value="30-SECOND-LAST">30-Second</option>
                                        <option value="1-MINUTE-LAST" selected>1-Minute</option>
                                        <option value="5-MINUTE-LAST">5-Minute</option>
                                        <option value="15-MINUTE-LAST">15-Minute</option>
                                        <option value="30-MINUTE-LAST">30-Minute</option>
                                        <option value="1-HOUR-LAST">1-Hour</option>
                                        <option value="custom">Custom</option>
                                    </select>
                                </div>

                                <div id="custom-bar-type-field-2" style={{ display: 'none' }}>
                                    <label className="text-sm text-muted-foreground">Custom Bar Type</label>
                                    <input type="text" placeholder="e.g., 2-SECOND-LAST" id="custom-bar-type-2"
                                        className="w-full mt-1 px-3 py-2 bg-white/5 border border-white/10 rounded-lg text-sm focus:outline-none focus:border-cyan-500" />
                                </div>

                                <div>
                                    <label className="text-sm text-muted-foreground">Timezone</label>
                                    <select id="timezone-2" className="w-full mt-1 px-3 py-2 bg-white/5 border border-white/10 rounded-lg text-sm focus:outline-none focus:border-cyan-500">
                                        <option value="UTC">UTC</option>
                                        <option value="US/Eastern">US/Eastern</option>
                                        <option value="Europe/London">Europe/London</option>
                                        <option value="Asia/Tokyo">Asia/Tokyo</option>
                                    </select>
                                </div>

                                <button onClick={ingestDirectory}
                                    className="w-full px-4 py-2 bg-cyan-600 hover:bg-cyan-700 text-white rounded-lg font-medium transition-colors">
                                    Batch Ingest Directory
                                </button>
                            </div>
                        </div>
                    </div>

                    {/* Available Data */}
                    <div className="p-4 rounded-xl bg-white/5 border border-white/10">
                        <h3 className="text-lg font-semibold mb-3">📊 Ingested Data</h3>
                        {Object.keys(availableData).length === 0 ? (
                            <p className="text-sm text-muted-foreground italic">No data ingested yet</p>
                        ) : (
                            <div className="grid gap-2 md:grid-cols-2 lg:grid-cols-3">
                                {Object.entries(availableData).map(([instrument, info]: [string, any]) => (
                                    <div key={instrument} className="p-3 rounded-lg bg-white/5 border border-white/10">
                                        <div className="font-semibold text-cyan-400">{instrument}</div>
                                        <div className="text-xs text-muted-foreground mt-1">
                                            {info.bar_count?.toLocaleString()} bars
                                        </div>
                                        <div className="text-xs text-muted-foreground">
                                            {info.start_date?.split('T')[0]} to {info.end_date?.split('T')[0]}
                                        </div>
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>
                </CardContent>
            </Card>

            {/* Configuration */}
            <Card variant="glass">
                <CardHeader>
                    <CardTitle>Backtest Configuration</CardTitle>
                </CardHeader>
                <CardContent>
                    <div className="p-6 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
                        <div>
                            <label className="text-sm text-muted-foreground">Start Date</label>
                            <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)}
                                className="w-full mt-1 px-3 py-2 bg-white/5 border border-white/10 rounded-lg text-sm focus:outline-none focus:border-cyan-500" />
                        </div>
                        <div>
                            <label className="text-sm text-muted-foreground">End Date</label>
                            <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)}
                                className="w-full mt-1 px-3 py-2 bg-white/5 border border-white/10 rounded-lg text-sm focus:outline-none focus:border-cyan-500" />
                        </div>
                        <div>
                            <label className="text-sm text-muted-foreground">Initial Balance</label>
                            <div className="flex gap-2 mt-1">
                                <input type="number" value={initialBalance} onChange={(e) => setInitialBalance(Number(e.target.value))}
                                    className="w-full px-3 py-2 bg-white/5 border border-white/10 rounded-lg text-sm focus:outline-none focus:border-cyan-500" />
                                <select value={currency} onChange={(e) => setCurrency(e.target.value)}
                                    className="w-24 px-3 py-2 bg-white/5 border border-white/10 rounded-lg text-sm focus:outline-none focus:border-cyan-500">
                                    <option value="USD">USD</option>
                                    <option value="EUR">EUR</option>
                                    <option value="GBP">GBP</option>
                                </select>
                            </div>
                        </div>
                        <div>
                            <label className="text-sm text-muted-foreground">Commission & Slippage</label>
                            <div className="flex gap-2 mt-1">
                                <input type="number" placeholder="Comm/Contract" id="backtest-commission" step="0.01"
                                    className="w-1/2 px-3 py-2 bg-white/5 border border-white/10 rounded-lg text-sm focus:outline-none focus:border-cyan-500" title="Commission per contract (e.g. 1.50)" />
                                <input type="number" placeholder="Slippage Prob" id="backtest-slippage" step="0.1" min="0" max="1"
                                    className="w-1/2 px-3 py-2 bg-white/5 border border-white/10 rounded-lg text-sm focus:outline-none focus:border-cyan-500" title="Probability of slippage (0.0 - 1.0)" />
                            </div>
                        </div>
                    </div>
                </CardContent>
            </Card>

            {/* Strategy Selection */}
            <Card variant="glass">
                <CardHeader>
                    <CardTitle>Select Strategies</CardTitle>
                </CardHeader>
                <CardContent>
                    <div className="space-y-2 mb-4">
                        {strategies.map(strategy => (
                            <div key={strategy.id} className="flex items-center gap-3 p-3 rounded-lg bg-white/5 hover:bg-white/8 transition-colors">
                                <input type="checkbox" checked={selectedStrategies.includes(strategy.id)}
                                    onChange={() => handleStrategyToggle(strategy.id)}
                                    className="w-4 h-4" />
                                <span className="flex-1">
                                    {strategy.config?.name || strategy.id} ({strategy.config?.strategy_type || 'Unknown'})
                                </span>
                                <button onClick={() => runBacktest(strategy.id)} disabled={loading}
                                    className="px-3 py-1 bg-emerald-600 hover:bg-emerald-700 disabled:bg-gray-600 text-white text-sm rounded transition-colors">
                                    Run
                                </button>
                            </div>
                        ))}
                    </div>
                    <button onClick={runMultipleBacktests} disabled={loading || selectedStrategies.length === 0}
                        className="w-full px-4 py-2 bg-cyan-600 hover:bg-cyan-700 disabled:bg-gray-600 text-white rounded-lg font-medium transition-colors">
                        Run Selected ({selectedStrategies.length})
                    </button>
                </CardContent>
            </Card>

            {/* Results */}
            <Card variant="glass">
                <CardHeader>
                    <CardTitle>Backtest Results</CardTitle>
                </CardHeader>
                <CardContent>
                    {backtestResults.length === 0 ? (
                        <p className="text-sm text-muted-foreground">No results yet</p>
                    ) : (
                        <div className="overflow-x-auto">
                            <table className="w-full text-sm">
                                <thead className="border-b border-white/10">
                                    <tr className="text-left">
                                        <th className="px-4 py-3">Strategy</th>
                                        <th className="px-4 py-3">Period</th>
                                        <th className="px-4 py-3 text-right">Trades</th>
                                        <th className="px-4 py-3 text-right">PnL</th>
                                        <th className="px-4 py-3">Actions</th>
                                    </tr>
                                </thead>
                                <tbody className="divide-y divide-white/5">
                                    {backtestResults.map(result => (
                                        <tr key={result.run_id} className="hover:bg-white/5">
                                            <td className="px-4 py-3">{result.strategy_id}</td>
                                            <td className="px-4 py-3 text-xs">{result.start_date} to {result.end_date}</td>
                                            <td className="px-4 py-3 text-right">{result.total_trades}</td>
                                            <td className="px-4 py-3 text-right">{result.statistics?.pnls?.Total || 'N/A'}</td>
                                            <td className="px-4 py-3">
                                                <div className="flex gap-2">
                                                    <button onClick={() => downloadTearsheet(result.run_id)}
                                                        className="px-2 py-1 bg-blue-600 hover:bg-blue-700 text-white text-xs rounded transition-colors">
                                                        HTML
                                                    </button>
                                                    <button onClick={() => downloadTradesCSV(result.run_id)}
                                                        className="px-2 py-1 bg-purple-600 hover:bg-purple-700 text-white text-xs rounded transition-colors">
                                                        CSV
                                                    </button>
                                                </div>
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    )}
                </CardContent>
            </Card>

            {/* PnL Comparison */}
            {backtestResults.length > 0 && (
                <Card variant="glass">
                    <CardHeader>
                        <CardTitle>PnL Comparison by Year</CardTitle>
                    </CardHeader>
                    <CardContent>
                        <div className="overflow-x-auto">
                            <table className="w-full text-sm">
                                <thead className="border-b border-white/10">
                                    <tr className="text-left">
                                        <th className="px-4 py-3">Strategy</th>
                                        {Array.from(new Set(backtestResults.flatMap(r => {
                                            const start = new Date(r.start_date).getFullYear();
                                            const end = new Date(r.end_date).getFullYear();
                                            return Array.from({ length: end - start + 1 }, (_, i) => start + i);
                                        }))).sort().map(year => (
                                            <th key={year} className="px-4 py-3 text-right">{year}</th>
                                        ))}
                                        <th className="px-4 py-3 text-right font-bold">Total</th>
                                    </tr>
                                </thead>
                                <tbody className="divide-y divide-white/5">
                                    {backtestResults.map(result => {
                                        const yearlyPnL = calculateYearlyPnL(result);
                                        const total = Object.values(yearlyPnL).reduce((a, b) => a + b, 0);

                                        return (
                                            <tr key={result.run_id} className="hover:bg-white/5">
                                                <td className="px-4 py-3">{result.strategy_id}</td>
                                                {Object.entries(yearlyPnL).map(([year, pnl]) => (
                                                    <td key={year} className={`px - 4 py - 3 text - right tabular - nums ${pnl >= 0 ? 'text-emerald-400' : 'text-red-400'} `}>
                                                        ${pnl.toFixed(2)}
                                                    </td>
                                                ))}
                                                <td className={`px - 4 py - 3 text - right font - bold tabular - nums ${total >= 0 ? 'text-emerald-400' : 'text-red-400'} `}>
                                                    ${total.toFixed(2)}
                                                </td>
                                            </tr>
                                        );
                                    })}
                                </tbody>
                            </table>
                        </div>
                    </CardContent>
                </Card>
            )}
        </div>
    );
};

export default Backtesting;
