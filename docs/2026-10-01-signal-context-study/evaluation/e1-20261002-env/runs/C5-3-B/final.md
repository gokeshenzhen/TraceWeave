已观察到 16 次写握手：DMA 写出数据、总线端口 0、`int_master_req[4]` 和 `master_req[4]` 的有效数据一致，grant 返回也一致。末次握手后 `dma_done` 拉高，下一拍状态回到 READY；我会继续核对 RAM 侧接收值、等待期间的数据保持，以及完成条件。
