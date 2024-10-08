import queue
import functools
from typing import List, Tuple

import jax
import jax.numpy as jnp
import jax.sharding as jsharding


class PageAttentionManager:
  """Manages page blocks.

  This manager maintains a main list of free page blocks, it support below features:
   1. Reseve pages for prefill insert and decode.
   2. Free pages resource for the slots after decode. Pages indices go to free list.
   3. Get pages indices meta data for all the slots.
   4. Transform and insert prefill caches to decode caches.
  """

  def __init__(
      self,
      batch_size: int,
      paged_attention_total_num_pages: int,
      paged_attention_page_size: int,
      max_pages_per_sequence: int,
  ):
    self.unused_pages = queue.Queue()
    self.batch_size = batch_size
    self.page_indices = jnp.full(
        (batch_size, max_pages_per_sequence),
        paged_attention_total_num_pages - 1,
        dtype=jnp.int32,
    )
    self.lengths = jnp.zeros(batch_size, dtype=jnp.int32)
    self.paged_attention_page_size = paged_attention_page_size
    self.max_pages_per_sequence = max_pages_per_sequence
    for i in range(paged_attention_total_num_pages):
      self.unused_pages.put(i, block=False)

  # pylint: disable-next=all
  def reserve_pages_insert(
      self, slot: int, seq_len: int
  ) -> Tuple[int, jax.Array]:
    self.lengths = self.lengths.at[slot].set(seq_len)
    num_pages = (
        seq_len // self.paged_attention_page_size
        if seq_len % self.paged_attention_page_size == 0
        else seq_len // self.paged_attention_page_size + 1
    )

    indices = [self.unused_pages.get(block=False) for _ in range(num_pages)]
    self.page_indices = self.page_indices.at[slot, :num_pages].set(indices)
    return num_pages, self.page_indices[slot, :num_pages]

  # pylint: disable-next=all
  def reserve_pages_decode(self, slot: int, seq_len: int):
    if seq_len > 0 and seq_len % self.paged_attention_page_size == 0:
      index = self.unused_pages.get(block=False)
      num_pages = seq_len // self.paged_attention_page_size
      self.page_indices = self.page_indices.at[slot, num_pages].set(index)

  # pylint: disable-next=all
  def fill_new_pages(self, lens: jax.Array):
    for slot in range(self.batch_size):
      self.reserve_pages_decode(slot, lens[slot])

  # pylint: disable-next=all
  def prefill_cache_padding(
      self,
      caches: List[Tuple[jax.Array, jax.Array]],
      seq_len: int,
      num_pages: int,
  ) -> List[Tuple[jax.Array, jax.Array]]:

    pad_width = num_pages * self.paged_attention_page_size - seq_len
    if pad_width == 0:
      return caches

    return [
        (self.pad_sequences(k, pad_width), self.pad_sequences(v, pad_width))
        for k, v in caches
    ]

  def insert_prefill_cache(
      self,
      prefill_caches: List[Tuple[jax.Array, jax.Array]],
      decode_caches: List[Tuple[jax.Array, jax.Array]],
      update_indexes: jax.Array,
      tep_kv: jax.Array,
      sharding: jsharding.Sharding,
  ) -> List[Tuple[jax.Array, jax.Array]]:
    """Insert prefill caches to decode caches.

    Args:
      prefill_caches: List of Tuple K, V. For each K, V:
        [batch_size, num_heads, seq_len, head_dim] jax.Array.
      decode_caches: List of Tuple K, V. For each K, V:
        [num_heads, paged_attention_total_num_pages, paged_attention_page_size, head_dim] jax.Array.
      update_indexes: Page indexes for insertion.
      tep_kv:  List of Tuple K, V. For each K, V:
        kv_heads, num_pages * .paged_attention_page_size, dim.
      sharding: Decode cache sharding.


    Returns:
      Decode cache. List of Tuple K, V. For each K, V:
        [num_heads, paged_attention_total_num_pages, paged_attention_page_size, head_dim] jax.Array.
    """
    # Reduce cache batch deminsion
    # [kv_heads, seq_len, dim]
    squeezed_caches = [
        (jnp.squeeze(k, axis=0), jnp.squeeze(v, axis=0))
        for k, v in prefill_caches
    ]
    tmp_caches = [
        (
            tep_kv.at[:, : k.shape[1], :].set(k),
            tep_kv.at[:, : v.shape[1], :].set(v),
        )
        for k, v in squeezed_caches
    ]
    kv_heads, _, dim = tmp_caches[0][0].shape
    # [kv_heads, num_pages, paged_attention_page_size, dim]
    paged_caches = [
        (
            jnp.reshape(k, (kv_heads, -1, self.paged_attention_page_size, dim)),
            jnp.reshape(v, (kv_heads, -1, self.paged_attention_page_size, dim)),
        )
        for k, v in tmp_caches
    ]

    @functools.partial(jax.jit, donate_argnums=(0, 1), inline=True)
    def insert(cache, new_entry):
      res = cache.at[:, update_indexes, :, :].set(new_entry)
      res = jax.lax.with_sharding_constraint(res, sharding)
      return res

    caches = [
        (insert(k, newk), insert(v, newv))
        for (k, v), (newk, newv) in zip(decode_caches, paged_caches)
    ]

    return caches

  # pylint: disable-next=all
  def get_page_token_indices(self, lens: jax.Array) -> jax.Array:
    # assert lens.shape == (
    #     self.batch_size,
    #     1,
    # ), f"len shape: {lens.shape} not equals batch size: {self.batch_size, 1}"
    update_page_indices = []
    token_scale_indices = []
    batch_slots = []
    offset = 0

    for slot in range(self.batch_size):
      seq_len = lens[slot]
      if seq_len == 0:
        continue
      num_pages = seq_len // self.paged_attention_page_size + 1
      token_pos = seq_len % self.paged_attention_page_size
      page_index = self.page_indices[slot, num_pages - 1]

      update_page_indices.append(page_index)
      token_scale_indices.append(offset + token_pos)
      batch_slots.append(slot)
      offset += self.paged_attention_page_size
    self.lengths = jnp.where(lens == 0, 0, lens + 1)
    update_page_indices = jnp.asarray(update_page_indices)
    token_scale_indices = jnp.asarray(token_scale_indices)
    batch_slots = jnp.asarray(batch_slots)
    return jnp.stack(
        (
            update_page_indices,
            token_scale_indices,
            batch_slots,
        )
    )

  # pylint: disable-next=all
  def get_compress_kv_cache(
      self,
      decode_caches: List[Tuple[jax.Array, jax.Array]],
      slot: int,
  ) -> List[Tuple[jax.Array, jax.Array]]:
    lens = self.lengths[slot]
    indices = self.page_indices[slot]
    return [
        (
            self._compress_cache(k, lens, indices),
            self._compress_cache(v, lens, indices),
        )
        for k, v in decode_caches
    ]

  def _compress_cache(self, cache: jax.Array, lens: int, indices: jax.Array):
    head, _, _, dim = cache.shape
    selected_cache = cache[:, indices, :, :]
    selected_cache = selected_cache.reshape((head, -1, dim))
    selected_cache = selected_cache[:, 0:lens, :]
    return selected_cache

  # pylint: disable-next=all
  def pad_sequences(self, array, pad_width=10):
    padding_config = [
        (0, 0),
        (0, 0),
        (0, pad_width),
        (0, 0),
    ]  # Pad only seq_len and dim
    padded_array = jnp.pad(array, padding_config, mode="constant")
    return padded_array

  # pylint: disable-next=all
  def free_pages_resource(self, slot):
    for i in range(self.max_pages_per_sequence):
      index = self.page_indices[slot, i]
      if index < 0:
        break
      self.unused_pages.put(index, block=False)

    self.page_indices = self.page_indices.at[slot, :].set(jnp.asarray([0]))
    return None
