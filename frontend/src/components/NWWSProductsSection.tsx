import React, { useEffect, useState, useCallback } from 'react';
import { apiUrl } from '../utils/api';

interface NWWSProduct {
  wmo_header: string | null;
  awips_id: string | null;
  office: string | null;
  product_type: string | null;
  preview: string;
  received_at: string;
  raw_length: number;
}

interface ProductsResponse {
  count: number;
  total_received: number;
  nwws_connected: boolean;
  products: NWWSProduct[];
}

const formatTime = (isoString: string): string => {
  const date = new Date(isoString);
  return date.toLocaleTimeString('en-US', {
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
    hour12: true,
  });
};

export const NWWSProductsSection: React.FC = () => {
  const [products, setProducts] = useState<NWWSProduct[]>([]);
  const [totalReceived, setTotalReceived] = useState(0);
  const [nwwsConnected, setNwwsConnected] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filterType, setFilterType] = useState('');
  const [filterOffice, setFilterOffice] = useState('');
  const [autoPoll, setAutoPoll] = useState(true);

  const fetchProducts = useCallback(async () => {
    try {
      const params = new URLSearchParams({ limit: '100' });
      if (filterType) params.set('product_type', filterType);
      if (filterOffice) params.set('office', filterOffice);

      const response = await fetch(apiUrl(`/api/nwws/products?${params}`));
      if (!response.ok) throw new Error(`HTTP ${response.status}`);

      const data: ProductsResponse = await response.json();
      setProducts(data.products);
      setTotalReceived(data.total_received);
      setNwwsConnected(data.nwws_connected);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch');
    } finally {
      setLoading(false);
    }
  }, [filterType, filterOffice]);

  useEffect(() => {
    fetchProducts();
    if (!autoPoll) return;
    const interval = setInterval(fetchProducts, 10_000);
    return () => clearInterval(interval);
  }, [fetchProducts, autoPoll]);

  return (
    <div className="section active nwws-products-section">
      <h2 className="section-title">NWWS Products</h2>

      <div className="nwws-controls">
        <div className="nwws-controls-left">
          <div className={`nwws-connection-badge ${nwwsConnected ? 'connected' : 'disconnected'}`}>
            <span className="nwws-connection-dot"></span>
            {nwwsConnected ? 'NWWS Connected' : 'NWWS Disconnected'}
          </div>
          <span className="nwws-total-count">{totalReceived.toLocaleString()} total products</span>
        </div>
        <div className="nwws-controls-right">
          <input
            className="nwws-filter-input"
            placeholder="Type filter..."
            value={filterType}
            onChange={(e) => setFilterType(e.target.value.toUpperCase())}
          />
          <input
            className="nwws-filter-input"
            placeholder="Office filter..."
            value={filterOffice}
            onChange={(e) => setFilterOffice(e.target.value.toUpperCase())}
          />
          <label className="nwws-autopoll-label">
            <input
              type="checkbox"
              checked={autoPoll}
              onChange={() => setAutoPoll(!autoPoll)}
            />
            Auto-refresh
          </label>
          <button onClick={fetchProducts} className="nwws-refresh-btn" disabled={loading}>
            <i className={`fas fa-sync-alt ${loading ? 'fa-spin' : ''}`}></i>
          </button>
        </div>
      </div>

      {error && (
        <div className="nwws-error">
          <i className="fas fa-exclamation-triangle"></i> {error}
        </div>
      )}

      <div className="nwws-product-list">
        {loading && products.length === 0 ? (
          <div className="nwws-loading">
            <i className="fas fa-spinner fa-spin"></i> Loading...
          </div>
        ) : products.length === 0 ? (
          <div className="nwws-empty">
            <i className="fas fa-inbox"></i>
            <p>No products received yet</p>
            {!nwwsConnected && <p>NWWS is not connected</p>}
          </div>
        ) : (
          products.map((product, i) => (
            <div key={`${product.received_at}-${i}`} className="nwws-product-item">
              <div className="nwws-product-header">
                <span className="nwws-product-awips">{product.awips_id || '???'}</span>
                {product.product_type && (
                  <span className="nwws-product-type">{product.product_type}</span>
                )}
                {product.office && (
                  <span className="nwws-product-office">{product.office}</span>
                )}
                <span className="nwws-product-time">{formatTime(product.received_at)}</span>
                <span className="nwws-product-size">{product.raw_length.toLocaleString()}b</span>
              </div>
              {product.wmo_header && (
                <div className="nwws-product-wmo">{product.wmo_header}</div>
              )}
              {product.preview && (
                <div className="nwws-product-preview">{product.preview}</div>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
};
