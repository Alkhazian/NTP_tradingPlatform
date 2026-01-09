import asyncio
from typing import Any
from decimal import Decimal

from nautilus_trader.adapters.interactive_brokers.data import InteractiveBrokersDataClient
from nautilus_trader.adapters.interactive_brokers.factories import InteractiveBrokersLiveDataClientFactory, get_cached_ib_client, get_cached_interactive_brokers_instrument_provider
from nautilus_trader.adapters.interactive_brokers.config import InteractiveBrokersInstrumentProviderConfig
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.enums import AssetClass

# IB API Tick Types - Must match Nautilus internals
BID_SIZE = 0
BID_PRICE = 1
ASK_PRICE = 2
ASK_SIZE = 3
LAST_PRICE = 4
LAST_SIZE = 5

class CustomInteractiveBrokersDataClient(InteractiveBrokersDataClient):
    """
    Custom Data Client that monkey-patches the underlying IB wrapper to handle
    Index (IND) data quirks, specifically mapping LAST price to QuoteTicks and masking invalid sizes.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Patch immediately upon creation
        self._patch_client()

    def _patch_client(self):
        try:
            client = self._client
            
            if getattr(client, "_is_patched_for_index", False):
                return
                
            original_process_tick_price = client.process_tick_price
            original_process_tick_size = client.process_tick_size
            
            def get_is_index(req_id):
                subscription = client._subscriptions.get(req_id=req_id)
                if not subscription:
                    return False, None
                
                try:
                    # Parse instrument_id from subscription name
                    inst_id_str = subscription.name
                    if isinstance(inst_id_str, tuple):
                        inst_id_str = inst_id_str[0]
                    
                    inst_id = InstrumentId.from_str(str(inst_id_str))
                    instrument = client._cache.instrument(inst_id)
                    
                    if instrument and instrument.asset_class == AssetClass.INDEX:
                        return True, subscription
                    
                    # Fallback check info for secType
                    if instrument and instrument.info and instrument.info.get('contract', {}).get('secType') == 'IND':
                        return True, subscription
                        
                except Exception as e:
                    pass
                
                return False, subscription

            async def custom_process_tick_price(req_id: int, tick_type: int, price: float, attrib: Any):
                is_index, subscription = get_is_index(req_id)
                
                # self._log.info(f"DEBUG: Tick req={req_id} type={tick_type} price={price} is_index={is_index}")
                
                if is_index:
                    if tick_type == 4: # LAST_PRICE
                        # self._log.info(f"DEBUG: Index LAST hit {price}")
                        # Optimized Logic:
                        # Directly update internal state for BOTH sides to prevent "flickering" single-sided ticks.
                        # Then trigger emission ONCE.
                        
                        if req_id not in client._subscription_tick_data:
                            client._subscription_tick_data[req_id] = {}
                        
                        tick_data = client._subscription_tick_data[req_id]
                        
                        # Pre-fill data
                        # Note: We must ensure we update the dictionary keys as integers
                        tick_data[BID_PRICE] = price
                        tick_data[ASK_PRICE] = price
                        # Inject default size 1 if missing or 0 to ensure emission
                        tick_data[BID_SIZE] = tick_data.get(BID_SIZE, 1) or 1
                        tick_data[ASK_SIZE] = tick_data.get(ASK_SIZE, 1) or 1
                        
                        # Call original logic for one side (e.g. BID) 
                        # This will set BID_PRICE again and trigger _try_create_quote_tick
                        await original_process_tick_price(req_id=req_id, tick_type=BID_PRICE, price=price, attrib=attrib)
                        return
                    elif tick_type in (BID_PRICE, ASK_PRICE):
                        # Suppress natural Bid/Ask ticks for Index as they are often zero/empty and corrupt our synthetic state
                        return

                await original_process_tick_price(req_id=req_id, tick_type=tick_type, price=price, attrib=attrib)

            async def custom_process_tick_size(req_id: int, tick_type: int, size: Decimal):
                is_index, subscription = get_is_index(req_id)
                
                if is_index:
                     # Override size to 1 to avoid "Ignoring invalid tick size" errors
                     size = Decimal(1)
                
                await original_process_tick_size(req_id=req_id, tick_type=tick_type, size=size)

            # Bind the custom methods to the client instance
            client.process_tick_price = custom_process_tick_price
            client.process_tick_size = custom_process_tick_size
            client._is_patched_for_index = True
            
        except Exception as e:
            self._log.exception(f"Error patching client: {e}", e)
            raise e
        
class CustomInteractiveBrokersLiveDataClientFactory(InteractiveBrokersLiveDataClientFactory):
    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: Any,
        msgbus: Any,
        cache: Any,
        clock: Any,
    ) -> InteractiveBrokersDataClient:
        # Use existing factory helper to get the shared IB client connection
        client = get_cached_ib_client(
            loop=loop,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            host=config.ibg_host,
            port=config.ibg_port,
            client_id=config.ibg_client_id,
            dockerized_gateway=config.dockerized_gateway,
        )

        # Use existing factory helper for instrument provider
        provider = get_cached_interactive_brokers_instrument_provider(
            client=client,
            clock=clock,
            config=config.instrument_provider,
        )

        # Create OUR Custom Data Client
        data_client = CustomInteractiveBrokersDataClient(
            loop=loop,
            client=client,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            ibg_client_id=config.ibg_client_id,
            config=config,
            name=name,
            connection_timeout=config.connection_timeout,
            request_timeout=config.request_timeout,
        )
        return data_client
