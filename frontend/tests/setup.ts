import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'
import { useTagManagerView } from '../src/store/tagManagerView'

afterEach(cleanup)

// The tag manager view store persists to localStorage, which jsdom keeps
// across tests within a file; restore the store defaults so no case inherits
// a previous case's filter, sort, page or session.
afterEach(() => {
  useTagManagerView.setState(useTagManagerView.getInitialState())
  window.localStorage.clear()
})
