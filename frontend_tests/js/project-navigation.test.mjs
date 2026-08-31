import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createContextualProjectNavigation } from '../../frontend/static/js/project.js';

test('工程内刷新与分支导航始终保留受管交付桥接', async () => {
  const completeManagedDelivery = async () => ({ status: 'PUBLISHED' });
  const managedDeliveryStatus = async () => ({ status: 'PUBLISHED' });
  const renders = [];
  const opens = [];
  const render = (view, options) => renders.push({ view, options });
  const open = async (projectId, operation, contextualRender) => {
    opens.push({ projectId, operation });
    contextualRender({ project_id: projectId }, { autostartRerun: true });
  };
  const navigation = createContextualProjectNavigation('project-1', {
    completeManagedDelivery,
    managedDeliveryStatus,
  }, { render, open });

  navigation.renderWithContext({ project_id: 'project-1' }, { autostartBootstrap: true });
  await navigation.openWithContext({ operation: 'refresh-after-final-job' });

  assert.deepEqual(opens, [{
    projectId: 'project-1',
    operation: { operation: 'refresh-after-final-job' },
  }]);
  assert.equal(renders.length, 2);
  for (const call of renders) {
    assert.equal(call.options.completeManagedDelivery, completeManagedDelivery);
    assert.equal(call.options.managedDeliveryStatus, managedDeliveryStatus);
  }
  assert.equal(renders[0].options.autostartBootstrap, true);
  assert.equal(renders[1].options.autostartRerun, true);
});
